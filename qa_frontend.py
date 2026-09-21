"""Static checks on the frontend that a syntax check cannot make.

`node --check` on app.js passes whether or not the code works, because a
function declared in the wrong scope is still valid JavaScript. That is not
hypothetical: `draftResting` was once declared inside `runForward`'s body, so
`LOAD.draft` called a name that did not exist at module scope and the Draft view
threw on navigation. The parse was clean. Only loading the page caught it.

So this checks the things that actually break a screen:

  * every handler named in LOAD, and every `run:` in the DEMO script, resolves
    to a function declared at module scope
  * every element id the code reaches for with $('#id') exists in index.html
  * every id index.html defines is unique
  * no figure is typed into the markup where a live one belongs

Run it before a demo. It needs nothing installed and touches no network.

    python qa_frontend.py
"""

import re
import sys

APP = "frontend/app.js"
HTML = "frontend/index.html"

# Language and browser names a call can legitimately resolve to.
JS_BUILTINS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "await",
    "async", "function", "new", "Promise", "Math", "Number", "String", "Array",
    "Object", "JSON", "Date", "Set", "Map", "parseInt", "parseFloat", "isNaN",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval", "fetch",
    "encodeURIComponent", "decodeURIComponent", "requestAnimationFrame",
    "getComputedStyle", "alert", "confirm", "console", "document", "window",
}

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def check(name: str, ok: bool, note: str = "") -> None:
    (PASS if ok else FAIL).append(name if ok else (name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {note}" if note else ""))


def module_scope_functions(src: str) -> set[str]:
    """Declarations at column zero — the only ones another top-level
    expression can reach."""
    names = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", src, re.M))
    names |= set(re.findall(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", src, re.M))
    return names


def main() -> None:
    app = open(APP, encoding="utf-8").read()
    html = open(HTML, encoding="utf-8").read()
    top = module_scope_functions(app)

    print("Frontend checks\n" + "=" * 15 + "\n")

    # ── every rendered table can be reached on a phone ────────────────────
    # A <table> is the one element on this console that is routinely wider than
    # a phone, and the sheet clips rather than scrolls — so a table without a
    # scrolling wrapper does not overflow visibly, it simply hides its right
    # hand columns with no way to reach them. That is invisible to a
    # document.scrollWidth check, which is how the "by buying ministry" table
    # shipped four of its five columns unreachable at 375px.
    #
    # Every other table on the console sits inside .tbl > .scroll. This asserts
    # the rule rather than trusting it: each `<table` written by the renderer
    # must have a `class="scroll"` opened before it and not yet closed.
    unwrapped = []
    for m in re.finditer(r"<table[ >]", app):
        before = app[max(0, m.start() - 1400):m.start()]
        if 'class="scroll"' not in before:
            line = app[: m.start()].count("\n") + 1
            unwrapped.append(f"app.js:{line}")
    check("every rendered table sits in a horizontal scroller",
          not unwrapped,
          f"unwrapped: {unwrapped[:5]}" if unwrapped else
          f"{len(re.findall(r'<table[ >]', app))} tables checked")

    # ── the view loaders actually exist ────────────────────────────────────
    block = re.search(r"const LOAD = \{(.*?)\n\};", app, re.S)
    missing = []
    if block:
        for ident in re.findall(r"=>\s*([A-Za-z_$][\w$]*)\s*\(", block.group(1)):
            if ident not in top:
                missing.append(ident)
        for ident in re.findall(r":\s*([A-Za-z_$][\w$]*)\s*[,}]", block.group(1)):
            if ident not in top and ident not in ("true", "false", "null"):
                missing.append(ident)
    check("every view loader is declared at module scope",
          bool(block) and not missing,
          f"unreachable: {sorted(set(missing))}" if missing else "")

    # ── the walkthrough's steps can run ────────────────────────────────────
    # Every bare call inside the walkthrough script, not just the one after
    # `run:` — matching the callee positionally meant the regex backtracked and
    # captured the word `async` out of `run: async () => {`. Checking the whole
    # block is both simpler and stricter: each step's body calls several
    # functions and all of them have to exist.
    demo = re.search(r"const DEMO = \[(.*?)\n\];", app, re.S)
    bad_run = []
    if demo:
        for ident in re.findall(r"(?<![.\w$'\"])([A-Za-z_$][\w$]*)\s*\(", demo.group(1)):
            if ident in JS_BUILTINS or ident in top:
                continue
            bad_run.append(ident)
    check("every function the Run demo script calls exists at module scope",
          bool(demo) and not bad_run,
          f"unreachable: {sorted(set(bad_run))}" if bad_run else
          "all calls resolve")

    # ── every id the script reaches for is in the page ─────────────────────
    # An id can come from the page or from a template literal the script
    # renders. Both are real; only an id that exists in neither is a selector
    # pointing at nothing.
    ids_in_html = set(re.findall(r'id="([^"]+)"', html))
    ids_in_js = set(re.findall(r'id="([A-Za-z0-9_-]+)"', app))
    ids_in_js |= set(re.findall(r'id="\$\{[^}]*\}"', app))      # computed ids, unverifiable
    wanted = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", app))
    absent = sorted(wanted - ids_in_html - ids_in_js)
    check("every element id the script selects exists somewhere",
          not absent, f"{len(absent)} selected but never created: {absent[:8]}" if absent else
          f"{len(wanted)} selectors checked against {len(ids_in_html)} page ids "
          f"and {len(ids_in_js)} rendered ids")

    # ── ids are unique ─────────────────────────────────────────────────────
    all_ids = re.findall(r'id="([^"]+)"', html)
    dupes = sorted({i for i in all_ids if all_ids.count(i) > 1})
    check("no id is defined twice", not dupes, f"duplicated: {dupes}" if dupes else "")

    # ── no live figure typed into the markup ───────────────────────────────
    # A number inside a data-ct / data-cv span is a placeholder that the API
    # overwrites; a stale one was on the hero for weeks saying 2,087 standards.
    typed = re.findall(r'data-c[tv]="[^"]+"\s*>([^<]+)<', html)
    numeric = [t.strip() for t in typed if re.search(r"\d", t)]
    check("live-figure placeholders carry no number",
          not numeric, f"typed figures: {numeric}" if numeric else
          f"{len(typed)} placeholders checked")

    # ── the hidden attribute actually hides ───────────────────────────────
    # A component with its own `display` outranks the user-agent rule for
    # [hidden], so an element the markup ships hidden renders anyway and the
    # code that sets `el.hidden = true` to dismiss it does nothing. That
    # shipped: the shortcuts overlay covered the page from load, empty, with a
    # close button that could not close it.
    css = open("frontend/styles.css", encoding="utf-8").read()
    has_rule = re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css)
    hidden_els = re.findall(r'id="([^"]+)"[^>]*\shidden', html)
    check("the hidden attribute outranks component display rules",
          bool(has_rule),
          "styles.css needs [hidden] { display: none !important }"
          if not has_rule else
          f"{len(hidden_els)} elements ship hidden and rely on it")

    print(f"\n{len(PASS)} passed · {len(FAIL)} failed")
    if FAIL:
        print("\nfailures:")
        for name, note in FAIL:
            print(f"  - {name}: {note}")
        sys.exit(1)


if __name__ == "__main__":
    main()
