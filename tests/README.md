# Test cases

Two kinds of tests live in this folder.

| | What it checks | Uses the real AI? |
|---|---|---|
| `test_*.py` (run with `python -m pytest`) | The code's logic: validation, answers, voice flow, settings. Gemini, Telegram and ElevenLabs are all faked. | No |
| `cases.json` + `run_eval.py` | How well Gemini reads realistic messages: typos, shorthand, Singlish, ranges and more. | Yes |

## Run the accuracy check

```powershell
C:\venvs\availability\Scripts\python -m tests.run_eval
```

Run it from the project folder, with a Gemini key in your `.env`. It prints PASS or FAIL for each case (with what it expected and what it got when it fails), then the totals by tag. It takes about 5 minutes, because the free Gemini tier allows only about 5 requests a minute. If Google is busy or the connection drops, that case is reported as `ERROR` and is not counted as a wrong answer, so run it again.

## Format of `cases.json`

```json
{
  "id": "wfh-tmr",
  "text": "wfh tmr",
  "sender": "Alex",
  "today": "2026-10-07",
  "tags": ["update", "relative-date", "shorthand"],
  "expected": {
    "intent": "update",
    "entries": [{ "person": "Alex", "date": "2026-10-08", "status": "wfh" }]
  }
}
```

| Field | Meaning |
|---|---|
| `id` | Short unique name for the case. |
| `text` | The message exactly as a user would type (or say) it. |
| `sender` | Who sent it. A message with no name in it ("wfh tmr") is about the sender. |
| `today` | The date to pretend it is, `YYYY-MM-DD`. It is fixed so that "tmr" and "next Wed" always mean the same day, whenever you run the cases. |
| `tags` | Groups for scoring, for example `update`, `query`, `singlish`, `typo`, `range`, `tricky`. |
| `expected` | The result the assistant should produce (see below). |

`expected` comes in three shapes:

- **An update** (the user is telling the assistant something): `{"intent": "update", "entries": [{"person", "date", "status"}, ...]}`. `status` is `office`, `wfh`, `half_day`, `leave`, or `clear` (erase what was entered for that day).
- **A query** (the user is asking): `{"intent": "query", "queries": [{"person", "start_date", "end_date"}, ...]}`. `person` is `null` when the question is about everyone. Dates are inclusive.
- **Unclear** (the assistant should ask a question back instead of guessing): `{"question": true}`. It passes if the assistant returns a question and no entries and no queries.

## Notes

- Entries are compared as sets, because their order doesn't matter.
- Results can vary a little between runs, so run it more than once.
- Some expected values encode a judgement. For example, "next Wed" on a Wednesday is taken to mean the Wednesday a week later, and "mc" counts as `leave`.
- `test_cases_file.py` checks that `cases.json` itself is well formed (valid dates and statuses, unique ids), so a typo in the data is caught before you run anything.
