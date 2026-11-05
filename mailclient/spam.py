"""Спам-фильтр: правила + наивный байесовский классификатор.

Порядок проверки письма:
1. Белый список (отправитель / тема / текст) -> не спам.
2. Чёрный список -> спам.
3. Заголовок X-Spam-Flag: YES от сервера -> спам.
4. Байес (если обучен хотя бы на MIN_DOCS спам- и не-спам-письмах).

Байес обучается, когда пользователь нажимает «Спам» / «Не спам».
"""
from __future__ import annotations

import math
import re
from email.utils import getaddresses
from dataclasses import dataclass

from .db import Database, Message
from .mailparse import header

MIN_DOCS = 5             # минимум писем каждого класса до включения байеса
MAX_TOKENS_PER_MSG = 400
TOKEN_RE = re.compile(r"[a-zа-яё0-9$€₽!][a-zа-яё0-9$€₽!'\-]{1,29}", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+\.)+[\w-]+")


@dataclass
class Verdict:
    is_spam: bool
    score: float | None
    reason: str


def tokenize(m: Message) -> set[str]:
    tokens: set[str] = set()
    sender = (m.sender or "").lower()
    found = EMAIL_RE.search(sender)
    if found:
        addr = found.group(0)
        tokens.add("from:" + addr)
        tokens.add("fromdomain:" + addr.split("@", 1)[1])
    for w in TOKEN_RE.findall((m.subject or "").lower()):
        tokens.add("subj:" + w)
    body = (m.body_text or "")[:20000].lower()
    for w in TOKEN_RE.findall(body):
        tokens.add(w)
        if len(tokens) >= MAX_TOKENS_PER_MSG:
            break
    if m.body_html and not m.body_text.strip():
        tokens.add("meta:html_only")
    if re.search(r"https?://", body):
        tokens.add("meta:has_url")
    return tokens


class SpamFilter:
    def __init__(self, db: Database):
        self.db = db

    # ---------- правила ----------
    @staticmethod
    def _field_value(m: Message, field: str) -> str:
        if field == "from":
            return m.sender or ""
        if field == "subject":
            return m.subject or ""
        return m.body_text or ""

    def _match_rules(self, m: Message, kind: str) -> str | None:
        for r in self.db.spam_rules():
            if r["kind"] != kind:
                continue
            if r["field"] in ("address", "domain"):
                wanted = r["pattern"].strip().casefold()
                addresses = [address.casefold() for _, address in getaddresses([m.sender]) if "@" in address]
                if r["field"] == "domain":
                    try:
                        wanted = wanted.lstrip("@").encode("idna").decode("ascii")
                        matches = any(address.rpartition("@")[2].encode("idna").decode("ascii") == wanted for address in addresses)
                    except UnicodeError:
                        matches = False
                else:
                    matches = wanted in addresses
                if matches:
                    return f"{'белый' if kind == 'white' else 'чёрный'} список: {r['field']} = «{r['pattern']}»"
            elif r["pattern"].lower() in self._field_value(m, r["field"]).lower():
                return f"{'белый' if kind == 'white' else 'чёрный'} список: {r['field']} содержит «{r['pattern']}»"
        return None

    # ---------- байес ----------
    def _counts(self) -> tuple[int, int]:
        return int(self.db.get_setting("bayes_spam_docs", "0")), int(self.db.get_setting("bayes_ham_docs", "0"))

    def bayes_ready(self) -> bool:
        s, h = self._counts()
        return s >= MIN_DOCS and h >= MIN_DOCS

    def bayes_score(self, m: Message) -> float:
        """Вероятность спама 0..1 (метод Робинсона / Фишера)."""
        n_spam, n_ham = self._counts()
        tokens = list(tokenize(m))
        if not tokens:
            return 0.5
        conn = self.db.conn()
        probs = []
        for i in range(0, len(tokens), 500):
            chunk = tokens[i:i + 500]
            rows = conn.execute(
                f"SELECT token, spam, ham FROM bayes_tokens WHERE token IN ({','.join('?' * len(chunk))})", chunk
            ).fetchall()
            for r in rows:
                sp = min(1.0, r["spam"] / max(n_spam, 1))
                hp = min(1.0, r["ham"] / max(n_ham, 1))
                if sp + hp == 0:
                    continue
                p = sp / (sp + hp)
                n = r["spam"] + r["ham"]
                # сглаживание: s=1, x=0.5
                p = (0.5 + n * p) / (1 + n)
                probs.append(p)
        if not probs:
            return 0.5
        # берём самые «уверенные» токены
        probs.sort(key=lambda p: abs(p - 0.5), reverse=True)
        probs = probs[:150]
        # комбинирование по Фишеру
        h = _chi2q(-2.0 * sum(math.log(max(p, 1e-200)) for p in probs), 2 * len(probs))
        s = _chi2q(-2.0 * sum(math.log(max(1.0 - p, 1e-200)) for p in probs), 2 * len(probs))
        return (1 + h - s) / 2

    def train(self, m: Message, as_spam: bool) -> None:
        """Обучить на письме. Если ранее обучали противоположно — откатываем."""
        target = 1 if as_spam else -1
        if m.trained_as == target:
            return
        if m.trained_as != 0:
            self._apply(m, m.trained_as == 1, -1)
        self._apply(m, as_spam, +1)
        if m.id is not None:
            self.db.update_message(m.id, trained_as=target)
        m.trained_as = target

    def _apply(self, m: Message, spam: bool, delta: int) -> None:
        col = "spam" if spam else "ham"
        key = "bayes_spam_docs" if spam else "bayes_ham_docs"
        with self.db.conn() as c:
            for t in tokenize(m):
                c.execute(f"INSERT INTO bayes_tokens(token, {col}) VALUES(?, 0) ON CONFLICT(token) DO NOTHING", (t,))
                c.execute(f"UPDATE bayes_tokens SET {col} = MAX(0, {col} + ?) WHERE token=?", (delta, t))
            cur = int(c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0])
            c.execute("UPDATE settings SET value=? WHERE key=?", (str(max(0, cur + delta)), key))

    # ---------- итоговое решение ----------
    def classify(self, m: Message) -> Verdict:
        reason = self._match_rules(m, "white")
        if reason:
            return Verdict(False, 0.0, reason)
        reason = self._match_rules(m, "black")
        if reason:
            return Verdict(True, 1.0, reason)
        if m.raw and header(m.raw, "X-Spam-Flag").strip().upper().startswith("YES"):
            return Verdict(True, 1.0, "сервер пометил X-Spam-Flag: YES")
        if self.bayes_ready():
            score = self.bayes_score(m)
            threshold = float(self.db.get_setting("spam_threshold", "0.9"))
            if score >= threshold:
                return Verdict(True, score, f"байес: {score:.2f} ≥ {threshold:.2f}")
            return Verdict(False, score, f"байес: {score:.2f}")
        return Verdict(False, None, "фильтр ещё не обучен")


def _chi2q(x2: float, v: int) -> float:
    """P(chi^2 >= x2) для v степеней свободы (v чётное)."""
    m = x2 / 2.0
    s = t = math.exp(-m)
    for i in range(1, v // 2):
        t *= m / i
        s += t
    return min(s, 1.0)
