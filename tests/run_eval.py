"""Evaluation runner: how well does Gemini read realistic messages?

For each message in tests/cases.json it asks the assistant to read it (llm.parse_schedule),
then checks the answer against the result we expected. At the end it prints how many
passed, overall and for each tag (typo, singlish, range, ...).

Run it from the project folder:
    C:\\venvs\\availability\\Scripts\\python -m tests.run_eval
It takes about 5 minutes, because the free Gemini tier allows only about 5 requests a minute.
"""

import json, time, datetime
import config, llm

config.load()   # reads your .env and users.json

data = json.load(open("tests/cases.json", encoding="utf-8"))


def entries_as_set(entries):
    """A list of {"person", "date", "status"} dicts as a set of tuples, so the order doesn't matter."""
    return {(e["person"], e["date"], e["status"]) for e in entries}


def queries_as_set(queries):
    """The same idea for questions: a set of (person, start_date, end_date)."""
    return {(q["person"], q["start_date"], q["end_date"]) for q in queries}


def is_correct(result, expected):
    """True if the assistant's result is what we expected."""
    # An unclear message: the right answer is to ask something back, and not to guess.
    if "question" in expected:
        asked_something = bool(result["question"])
        return asked_something == expected["question"] and not result["entries"] and not result["queries"]

    # Otherwise it must have understood whether the user was telling it or asking it...
    if result["intent"] != expected["intent"]:
        return False
    # ...and got the details right.
    if expected["intent"] == "update":
        return entries_as_set(result["entries"]) == entries_as_set(expected["entries"])
    return queries_as_set(result["queries"]) == queries_as_set(expected["queries"])


passes = 0
fails = 0
errors = []          # cases where Gemini couldn't be reached: these are not counted as wrong answers
by_tag = {}          # tag -> [number passed, number run]

for case in data["cases"]:
    today = datetime.date.fromisoformat(case["today"])
    try:
        result = llm.parse_schedule(case["text"], today, case["sender"])
    except llm.LLMError as error:
        print("ERROR", case["id"], "-", error)
        errors.append(case["id"])
        time.sleep(13)
        continue

    correct = is_correct(result, case["expected"])
    print("PASS" if correct else "FAIL", case["id"], "-", repr(case["text"]))
    if not correct:
        print("     expected:", case["expected"])
        print("     got:     ", result)

    if correct:
        passes += 1
    else:
        fails += 1
    for tag in case["tags"]:
        counts = by_tag.setdefault(tag, [0, 0])
        counts[0] += correct      # True counts as 1
        counts[1] += 1

    time.sleep(13)                # the free tier allows about 5 calls a minute

print()
print("Results by tag (a case can have several tags, so these overlap):")
for tag in sorted(by_tag):
    done, total = by_tag[tag]
    print(f"  {tag:<16} {done}/{total}")
print()
print(f"TOTAL: {passes}/{passes + fails} passed")
if errors:
    print(f"{len(errors)} case(s) couldn't be run and are not counted: {errors}")
