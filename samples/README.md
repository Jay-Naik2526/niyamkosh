# Sample tenders for the "Fix my document" screen

Two versions of the same fictional tender — one Word, one PDF — written to
exercise every kind of change the repair can make. The department, tender
number and officer are invented; the IS numbers and their status are real.

Upload either one at **Officer → Fix my document**.

## What is wrong with them, and what should happen

| In the document | What the console should do | Why |
|---|---|---|
| `IS 1753` | replace with `IS 8130` | IS 8130's own BIS record says it supersedes IS 1753 |
| `IS 4759` | replace with `IS 4736`, marked *check this* | BIS records no successor; this is the closest current standard, offered as a candidate |
| `IS 1570` | left in place, **marked for you** | BIS records no successor and nothing is close enough to suggest — so it is not silently replaced or deleted |
| no ISI mark clause | clause added | the document buys a product under a Quality Control Order and never demands the Standard Mark |
| — | `IS 5831`, `IS 7098`, `IS 2062` added | cited by comparable real tenders alongside what this one already cites |

The expected result is **5 blocking problems before, 1 after** — and the screen
says "Corrected, not yet clear", because IS 1570 still needs a human. That is
the intended outcome, not a failure: the second audit reports what is actually
in the produced file.

## Things to look for

- The `.docx` splits `IS 1753` across three runs, the way Word really stores
  text that has been edited. It is still found and replaced, including inside
  the schedule table.
- Open the corrected `.docx`: the emblem, header, footer, table and signature
  block are the ones you uploaded.
- Open the corrected `.pdf`: the citations are redrawn in the document's own
  typeface, because the page's content stream was edited rather than covered.
- Download both formats. Only the one matching your upload keeps the letterhead;
  the other says so.
