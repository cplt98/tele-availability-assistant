"""Turns a plain-language message into either schedule entries (someone TELLING
the bot their schedule) or queries (someone ASKING about a schedule).

The rest of the bot only calls parse_schedule(text, today, sender). To switch to
a different AI provider later, rewrite _call_model() and nothing else.

The model only *suggests*. validate() checks every name, date and status in code.
Entries are only saved by bot.py after the user presses Yes, and answers to
queries come from the database, never from the model.
"""

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel

import config
import db
import holidays_sg

logger = logging.getLogger("llm")

# An entry with this "status" means "erase what was entered for that day".
CLEAR = "clear"

TIMEZONE = ZoneInfo("Asia/Singapore")

# Dates further away than this from today are treated as a misread.
MAX_DAYS_BACK = 7          # for entries being saved
MAX_QUERY_DAYS_BACK = 60   # people may ask about the past ("was sam in last Friday?")
MAX_DAYS_AHEAD = 366
MAX_QUERY_SPAN = 31        # longest range one question may cover, in days


class LLMError(Exception):
    """The model call failed (network, rate limit, bad reply...)."""


def today_sg():
    """Today's date in Singapore time, whatever timezone this PC is set to."""
    return datetime.now(TIMEZONE).date()


# ---------- The response schema the model must follow ----------

class Entry(BaseModel):
    person: Optional[str] = None  # who it's for; empty means the person who sent the message
    date: str  # YYYY-MM-DD
    status: Literal["office", "wfh", "half_day", "leave", "clear"]


class Query(BaseModel):
    person: Optional[str] = None  # a name, "everyone", or empty for the sender themselves
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD, inclusive (same as start_date for a single day)


class ScheduleResult(BaseModel):
    intent: Literal["update", "query"]
    entries: list[Entry]
    queries: list[Query]
    question: Optional[str] = None


# ---------- The prompt ----------

def build_system_prompt(today, sender="the sender"):
    """Instructions for the model, including a small calendar so it doesn't
    have to work out weekdays in its head."""
    calendar = "\n".join(
        f"  {d:%a} {d.isoformat()}" for d in (today + timedelta(days=i) for i in range(15))
    )
    monday = today - timedelta(days=today.weekday())
    this_week = ", ".join(f"{d:%a} {d.isoformat()}" for d in (monday + timedelta(days=i) for i in range(5)))
    next_monday = monday + timedelta(days=7)
    next_week = ", ".join(f"{d:%a} {d.isoformat()}" for d in (next_monday + timedelta(days=i) for i in range(5)))
    upcoming = holidays_sg.public_holidays(today, today + timedelta(days=366))
    public = "\n".join(f"  {date.fromisoformat(d):%a} {d}: {name}" for d, name in upcoming.items()) or "  (none)"

    return f"""You read a team member's message about work schedules. The message is an UPDATE (they are telling you a schedule) or a QUERY (they are asking about one).

Today is {today:%A} {today.isoformat()} (Asia/Singapore time).
Calendar for the next two weeks:
{calendar}
The current work week (Mon-Fri) is: {this_week}
Next work week (Mon-Fri) is: {next_week}
Singapore public holidays in the next year (built in, nobody needs to enter them):
{public}

Step 1: set "intent".
- "query": the message ASKS about a schedule. Examples: "is sam wfh today", "where is sam tomorrow", "sam's schedule this week", "who is in the office friday", "is sam in office thu or wfh", "check what sam has next week". Questions very often have NO question mark, so judge by meaning and wording: a question word (is, are, does, where, what, when, who, which, how), a leading "is/are/does", "check", "schedule for", "or not", and so on.
- "update": the message TELLS you a schedule. A short statement with no question wording, like "sam wfh friday", "wfh tomorrow" or "office Mon to Wed", is an update.
- Public holidays are already built in. If someone tells you a public holiday, or asks about holidays themselves ("is monday a public holiday"), return no entries or queries and put ONE short line in "question": public holidays already show up when you ask about someone's schedule.
- If it is truly unclear whether they are telling or asking, return no entries and no queries and ask ONE short question in "question", such as "Do you want to check Sam's schedule, or tell me it?".
- For a query, leave "entries" empty. For an update, leave "queries" empty.

Queries: each one has "person" (a name as written, or "everyone" for the whole group, or empty if the sender asks about themselves), "start_date" and "end_date" (inclusive, YYYY-MM-DD). One day means start_date equals end_date. "This week" ALWAYS means the whole current work week, from its Monday to its Friday, even if today is later in the week. "Next week" means the whole of next work week, Monday to Friday. "Today" and "tomorrow" come from the calendar above. Use one query per person if several people are asked about.

The message was sent by "{sender}". Each update entry says who it is for in "person":
- If the message names someone ("sam is WFH Friday", "Jordan office Mon"), use that name exactly as written, e.g. "Sam" or "Jordan". The sender may be writing about someone else.
- If nobody is named, or the sender says "I", "me" or "my", leave "person" empty.
- One message can be about several people.

Statuses (use exactly these values):
- "office": working in the office
- "wfh": working from home
- "half_day": a half day
- "leave": on leave (annual leave, day off, MC, taking leave). "Half day leave" counts as "half_day".
- "clear": erase what was entered for that day because they changed their mind or are not sure any more ("clear 8 Dec", "remove my status for Friday", "not sure about Mon anymore", "cancel my leave on 8 Dec"). To CHANGE a day to a different status, just give the new status; no "clear" is needed.

Rules:
- For ranges and "rest of the week", skip the public holidays listed above unless the user names that day.
- Return every date as YYYY-MM-DD, using the calendar above.
- A date with no year (like "1 Oct") means the next upcoming one, counting today.
- A bare weekday ("Thu") means the nearest upcoming one, counting today. "next Thu" means the nearest one after today.
- A range like "office Mon to Wed" means every day in it, so Mon, Tue and Wed are three entries.
- A message can hold several statuses: "office Mon Tue, half day Wed, rest WFH" means Mon and Tue office, Wed half_day, and the rest of the current work week (Thu, Fri) wfh.
- Only include work days the user actually mentioned or clearly implied. One entry per person per date.
- If something important is unclear (which week, an unknown status, no dates at all), return no entries and put ONE short, friendly question in "question". Otherwise leave "question" empty.
- A status with no day or date at all (just "wfh", "office", "half day" or "on leave") is unclear: do NOT assume today, return no entries and ask which day.
- If the message has nothing to do with a schedule, return no entries and a "question" saying what you can help with.
- The message may include earlier conversation (a question you asked, a proposed schedule the user wants changed). Use it, and return the final complete list of entries."""


# ---------- The one provider-specific function ----------

def _call_model(text, today, sender):
    """Send the message to Gemini and return its reply as JSON text.

    This is the only function that knows about Gemini. Swap providers here.
    """
    from google import genai
    from google.genai import types

    try:
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=build_system_prompt(today, sender),
                response_mime_type="application/json",
                response_schema=ScheduleResult,
                temperature=0,
            ),
        )
        return response.text
    except Exception as e:  # rate limits, network problems, bad key, blocked reply...
        raise LLMError(f"{type(e).__name__}: {e}") from e


# ---------- Checking what the model said ----------

SELF_WORDS = {"i", "me", "my", "myself", "sender"}
EVERYONE_WORDS = {"everyone", "everybody", "all", "anyone", "team", "the team", "group", "the group"}


def normalize_person(name):
    """Tidy a name so "sam", "SAM " and "Sam" are all stored as "Sam".

    Returns None if it doesn't look like a name.
    """
    name = " ".join(str(name).split()).title()
    if not name or len(name) > 30 or not re.fullmatch(r"[\w '.-]+", name):
        return None
    return name


def _clean_entry(item, today, sender):
    """Return {"person", "date", "status"} if the entry is valid, otherwise None."""
    if not isinstance(item, dict):
        return None

    person = item.get("person")
    if person is None or not str(person).strip() or str(person).strip().lower() in SELF_WORDS:
        person = sender  # nobody named: it's about the person who sent the message
    person = normalize_person(person)
    if person is None:
        return None

    status = item.get("status")
    if status not in db.VALID_STATUSES and status != CLEAR:
        return None
    try:
        day = datetime.strptime(str(item.get("date")), "%Y-%m-%d").date()
    except ValueError:
        return None
    if not (today - timedelta(days=MAX_DAYS_BACK) <= day <= today + timedelta(days=MAX_DAYS_AHEAD)):
        return None
    return {"person": person, "date": day.isoformat(), "status": status}


def _clean_query(item, today, sender):
    """Return {"person", "start_date", "end_date"} if valid, otherwise None.

    "person" is None when the question is about everyone.
    """
    if not isinstance(item, dict):
        return None

    person = item.get("person")
    if person is None or not str(person).strip() or str(person).strip().lower() in SELF_WORDS:
        person = sender
    elif str(person).strip().lower() in EVERYONE_WORDS:
        person = None
    else:
        person = normalize_person(person)
        if person is None:
            return None

    try:
        start = datetime.strptime(str(item.get("start_date")), "%Y-%m-%d").date()
        end = datetime.strptime(str(item.get("end_date")), "%Y-%m-%d").date()
    except ValueError:
        return None
    if start > end or (end - start).days >= MAX_QUERY_SPAN:
        return None
    if start < today - timedelta(days=MAX_QUERY_DAYS_BACK) or end > today + timedelta(days=MAX_DAYS_AHEAD):
        return None
    return {"person": person, "start_date": start.isoformat(), "end_date": end.isoformat()}


def _result(intent, entries=None, queries=None, question=None):
    """Every result has the same keys, whatever the intent."""
    return {
        "intent": intent,
        "entries": entries or [],
        "queries": queries or [],
        "question": question,
    }


def validate(raw_text, today, sender):
    """Check the model's JSON.

    Returns {"intent": "update" or "query", "entries": [...], "queries": [...],
    "question": str or None}. Bad items are never passed on: if any are invalid we
    ask the user to rephrase instead of guessing.
    """
    try:
        data = json.loads(raw_text)
    except (TypeError, ValueError) as e:
        raise LLMError(f"Model did not return valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMError("Model reply was not a JSON object")

    question = data.get("question")
    question = question.strip() if isinstance(question, str) and question.strip() else None

    if data.get("intent") == "query":
        return _validate_query(data, question, today, sender)
    return _validate_update(data, question, today, sender)  # anything else counts as an update


def _validate_query(data, question, today, sender):
    """The message was someone asking about a schedule."""
    raw_queries = data.get("queries") or []
    if not isinstance(raw_queries, list):
        raise LLMError("Model 'queries' was not a list")

    queries = [_clean_query(item, today, sender) for item in raw_queries]
    if any(q is None for q in queries):
        logger.warning("Model returned invalid queries: %s", raw_queries)
        return _result("query", question=question or "I couldn't tell who or which days you mean. Could you say it again, e.g. 'Is Sam WFH on Friday?'")

    if queries:
        # We can answer, so any "question" is just the model chatting. Answer instead of asking.
        question = None
    elif not question:
        question = "I couldn't tell who or which days you mean. Try something like 'Is Sam WFH today?'"
    return _result("query", queries=queries, question=question)


def _validate_update(data, question, today, sender):
    """The message was someone telling us a schedule."""
    raw_entries = data.get("entries") or []
    if not isinstance(raw_entries, list):
        raise LLMError("Model 'entries' was not a list")

    by_key = {}  # one entry per (person, date); if the model repeats one, the last wins
    any_bad = False
    for item in raw_entries:
        entry = _clean_entry(item, today, sender)
        if entry is None:
            any_bad = True
        else:
            by_key[(entry["person"], entry["date"])] = entry["status"]

    if any_bad:
        logger.warning("Model returned invalid entries: %s", raw_entries)
        return _result("update", question=question or "I couldn't read some of those names, dates or statuses. Could you say it again, e.g. 'Sam WFH on 1 Oct'?")

    if not by_key and not question:
        question = "I couldn't find a schedule in that. Try something like 'WFH tomorrow' or 'Sam office Mon to Wed'."

    entries = [
        {"person": p, "date": d, "status": s}
        for (p, d), s in sorted(by_key.items())
    ]
    return _result("update", entries=entries, question=question)


def parse_schedule(text, today, sender):
    """Main entry point. `sender` is the name of the person who sent the message.

    Returns {"intent": "update" or "query",
             "entries": [{"person", "date", "status"}, ...],   (for updates)
             "queries": [{"person", "start_date", "end_date"}, ...],   (for queries; person None = everyone)
             "question": str or None}.
    Raises LLMError if the model can't be reached or replies with nonsense.
    """
    raw = _call_model(text, today, sender)
    return validate(raw, today, sender)
