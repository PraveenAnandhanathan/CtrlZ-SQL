"""A cache key that ignores literal values without ever changing a verdict.

The interceptor memoises verdicts on exact statement text, which works for
parameterised clients and not at all for the ones that interpolate. psycopg2
sends ``WHERE id = 5`` and then ``WHERE id = 6``; those never share an entry, so
every statement is a first look and the memo is dead weight. Measured, that is
0.31 ms per statement instead of 0.001 ms (NFR-2).

Blanking literals is the obvious fix and the obvious fix is wrong. Of everything
the policy engine reads, exactly one field depends on literal values --
``filter_is_tautology`` -- and it is the field that decides whether
``WHERE 1 = 1`` is a filter or a table-wide write:

    WHERE 1 = 1     tautology     -> treated as unfiltered
    WHERE 1 = 2     not tautology -> an ordinary filter
    WHERE 'a' = 'a' tautology
    WHERE id = 5    not tautology

Replace the literals with placeholders and the first two become the same string.
Whichever arrived first would then answer for the other, so a statement that
wipes a table could inherit "allowed" from one that matches nothing. That is the
whole class of bug this module has to avoid, and it is why the rule here is not
"normalise literals" but:

    **Normalise literals only when no literal is compared against another
    literal. Otherwise use the statement verbatim.**

A literal compared to a column or an expression cannot make a tautology, so
blanking it is safe. A literal compared to another literal is precisely the
tautology-or-contradiction case, and those statements simply keep their exact
text as the key -- costing nothing, because hand-written ``WHERE 1 = 1`` is rare
and a real client sending it repeatedly still gets a cache hit on the raw text.

The conservative direction matters: being wrong here means a wrong verdict, and
declining to normalise only means a slower one.
"""

from __future__ import annotations

#: What a literal is replaced with. Not valid SQL, deliberately -- this string
#: is a cache key and must never be mistaken for something executable.
PLACEHOLDER = "\x00L\x00"

#: Operators that can put two literals in a comparison. `IS` and `LIKE` are
#: words and handled separately.
_COMPARISONS = ("<=", ">=", "<>", "!=", "=", "<", ">")

_WORD_COMPARISONS = ("is", "like", "ilike", "between", "in")

_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")


class _Literal:
    """A literal found in the statement, and where it sat."""

    __slots__ = ("start", "end")

    def __init__(self, start: int, end: int):
        self.start = start
        self.end = end


def fingerprint(sql: str) -> str:
    """A key under which this statement's verdict may safely be reused.

    Returns the statement with literal values replaced, or the statement
    unchanged when replacing them could alter what the rules read.
    """
    literals = _find_literals(sql)
    if not literals:
        return sql
    if _compares_two_literals(sql, literals):
        return sql

    out = []
    previous = 0
    for literal in literals:
        out.append(sql[previous:literal.start])
        out.append(PLACEHOLDER)
        previous = literal.end
    out.append(sql[previous:])
    return "".join(out)


def _find_literals(sql: str) -> list[_Literal]:
    """Locate string and numeric literals, skipping everything that only looks
    like one: quoted identifiers, comments, and bind placeholders."""
    literals: list[_Literal] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        # Quoted identifiers are names, not values. Step over them untouched.
        if char == '"':
            index = _skip_quoted(sql, index, '"')
            continue
        if char == "`":
            index = _skip_quoted(sql, index, "`")
            continue

        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue
        if char == "/" and sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            index = length if close == -1 else close + 2
            continue

        if char == "'":
            end = _skip_quoted(sql, index, "'")
            literals.append(_Literal(index, end))
            index = end
            continue

        if char == "$":
            end = _dollar_quote_end(sql, index)
            if end is not None:
                literals.append(_Literal(index, end))
                index = end
                continue
            # $1, $2 -- a bind parameter. Already constant text; leave it.
            index += 1
            while index < length and sql[index].isdigit():
                index += 1
            continue

        if char.isdigit():
            # A digit inside an identifier (`col1`, `t2`) is part of the name.
            if index > 0 and sql[index - 1] in _IDENT_CHARS:
                index += 1
                continue
            end = _number_end(sql, index)
            literals.append(_Literal(index, end))
            index = end
            continue

        index += 1

    return literals


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    """Index just past a quoted run, honouring doubling as the escape."""
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == "\\" and quote == "'":
            index += 2          # backslash escapes, as PostgreSQL's E'' allows
            continue
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:
                index += 2      # '' inside a string is one quote
                continue
            return index + 1
        index += 1
    return length               # unterminated: the server's problem, not ours


def _dollar_quote_end(sql: str, start: int) -> "int | None":
    """Index just past a ``$tag$ ... $tag$`` literal, or None if not one."""
    close = sql.find("$", start + 1)
    if close == -1:
        return None
    tag = sql[start:close + 1]
    if any(char not in _IDENT_CHARS for char in tag[1:-1]):
        return None
    if tag[1:-1].isdigit():
        return None             # $1$ is not a dollar quote in any dialect
    end = sql.find(tag, close + 1)
    if end == -1:
        return None
    return end + len(tag)


def _number_end(sql: str, start: int) -> int:
    """Index just past a numeric literal, including a decimal and exponent."""
    index = start
    length = len(sql)
    while index < length and sql[index].isdigit():
        index += 1
    if index < length and sql[index] == ".":
        index += 1
        while index < length and sql[index].isdigit():
            index += 1
    if index < length and sql[index] in "eE":
        lookahead = index + 1
        if lookahead < length and sql[lookahead] in "+-":
            lookahead += 1
        if lookahead < length and sql[lookahead].isdigit():
            index = lookahead
            while index < length and sql[index].isdigit():
                index += 1
    return index


def _compares_two_literals(sql: str, literals: list[_Literal]) -> bool:
    """Whether any literal is compared directly against another literal.

    The check is over the text *between* consecutive literals: if it holds
    nothing but whitespace, a comparison operator, and optionally `NOT`, the two
    are being compared and the statement must not be normalised.

    Deliberately crude in the safe direction. A false positive costs a cache
    entry; a false negative costs a wrong verdict.
    """
    for earlier, later in zip(literals, literals[1:]):
        between = sql[earlier.end:later.start].strip().lower()
        if not between:
            # Adjacent with nothing between them is not a comparison, but it is
            # not something we understand either. Decline.
            return True
        if between in _COMPARISONS:
            return True
        words = between.replace("(", " ").replace(")", " ").split()
        if words and words[0] == "not":
            words = words[1:]
        if len(words) == 1 and words[0] in _WORD_COMPARISONS:
            return True
        if len(words) == 1 and words[0] in _COMPARISONS:
            return True
    return False
