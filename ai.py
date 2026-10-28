"""Gemini helpers for SmartStudy AI.

Every generator goes through `_generate`, which adds a timeout, retries and
output validation, so a flaky network or a malformed response gets a second
chance before the user ever sees an error.
"""
import difflib
import json
import logging
import os
import random
import time

log = logging.getLogger(__name__)


class AIError(RuntimeError):
    """Gemini is unavailable or returned unusable output."""


def _model():
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")


def _generate(prompt, schema, temperature, build):
    """Call Gemini for JSON, pass it to `build` (raises ValueError if invalid), retry on failure."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise AIError("Gemini API key is missing. Add GEMINI_API_KEY to your .env file and restart SmartStudy AI.")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise AIError("The Gemini SDK is not installed. Run: pip install google-genai") from exc

    timeout_ms = int(float(os.getenv("GEMINI_TIMEOUT_SECONDS", "60")) * 1000)
    attempts = max(1, int(os.getenv("GEMINI_RETRIES", "2")))
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout_ms))
    last = None
    for attempt in range(attempts):
        try:
            response = client.models.generate_content(
                model=_model(),
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            return build(json.loads(response.text or "{}"))
        except Exception as exc:  # network, quota, bad JSON, failed validation
            last = exc
            log.warning("Gemini attempt %d/%d failed: %s", attempt + 1, attempts, exc)
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise AIError(f"Gemini could not complete the request after {attempts} attempt(s): {last}") from last


# ---------- shared validation ----------

_QUESTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "question": {"type": "STRING"},
        "options": {"type": "ARRAY", "items": {"type": "STRING"}},
        "answer": {"type": "INTEGER"},
        "topic": {"type": "STRING"},
        "explanation": {"type": "STRING"},
        "question_type": {"type": "STRING"},
    },
    "required": ["question", "options", "answer", "topic", "explanation"],
}


def _snap_topic(topic, choices):
    """Map a model-written topic label onto the closest known label so stats don't fragment."""
    by_key = {c.casefold(): c for c in choices}
    if topic.casefold() in by_key:
        return by_key[topic.casefold()]
    close = difflib.get_close_matches(topic.casefold(), list(by_key), n=1, cutoff=0.6)
    return by_key[close[0]] if close else topic


def _clean_question(q, default_topic, topic_choices=None):
    """Validate one question; shuffle options so correct answers are evenly spread."""
    if not isinstance(q, dict):
        return None
    text = str(q.get("question") or "").strip()
    options = q.get("options")
    try:
        answer = int(q.get("answer", -1))
    except (TypeError, ValueError):
        return None
    if not text or not isinstance(options, list) or len(options) != 4 or answer not in range(4):
        return None
    options = [str(o).strip() for o in options]
    if not all(options) or len({o.casefold() for o in options}) < 4:
        return None
    correct = options[answer]
    random.shuffle(options)
    topic = str(q.get("topic") or default_topic).strip() or default_topic
    if topic_choices:
        topic = _snap_topic(topic, topic_choices)
    cleaned = {
        "question": text,
        "options": options,
        "answer": options.index(correct),
        "topic": topic,
        "explanation": str(q.get("explanation") or "").strip(),
    }
    if q.get("question_type"):
        cleaned["question_type"] = str(q["question_type"]).strip()
    return cleaned


def _strings(values):
    return [str(v).strip() for v in (values or []) if str(v).strip()]


# ---------- learning kit ----------

def make_kit(title, notes, scope=""):
    """Build a teach-first lesson kit plus a quiz.

    `scope` is placed ahead of the notes, so it is never lost to truncation.
    """
    notes = notes[:15000]
    scope_block = f"\nSCOPE (follow strictly):\n{scope}\n" if scope else ""
    prompt = f"""You are an excellent human tutor and assessment designer. Build a genuinely useful mini-lesson from the source notes.

SOURCE RULES:
- Treat the notes as the primary source. Do not introduce unsupported facts, definitions, equations, or claims.
- You may clarify in simpler language and add a small illustrative example, clearly labelled and consistent with the source.
- If the notes lack detail for a point, say so briefly rather than inventing facts.
- Never mention AI, the prompt, or "the source text" in student-facing content.

TEACHING QUALITY:
- Explain like a tutor: core idea first, then how/why it works and how concepts connect.
- Avoid boilerplate such as "This is important because" in every section.
- Stay specific to the topic; do not just rephrase each sentence of the notes.

QUIZ QUALITY:
- Create exactly 8 multiple-choice questions with exactly 4 options each.
- Mix: 2 concept checks, 3 application/scenario, 2 reasoning/comparison, 1 synthesis (adjust if the source cannot support them).
- No "According to the text..." questions. No "all/none of the above", joke answers, giveaways or duplicates.
- Distractors must be plausible misconceptions of similar length and grammatical form.
- One unambiguously correct answer, a concise explanation, and a topic label.
- IMPORTANT: each question's `topic` must be copied EXACTLY from the `title` of one of the concepts you wrote.
{scope_block}
Lesson title: {title}
Lesson notes:
{notes}
"""
    schema = {
        "type": "OBJECT",
        "properties": {
            "summary": {"type": "STRING"},
            "objectives": {"type": "ARRAY", "items": {"type": "STRING"}},
            "concepts": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "title": {"type": "STRING"}, "explanation": {"type": "STRING"}, "example": {"type": "STRING"}},
                "required": ["title", "explanation", "example"]}},
            "key_terms": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "term": {"type": "STRING"}, "definition": {"type": "STRING"}}, "required": ["term", "definition"]}},
            "recap": {"type": "ARRAY", "items": {"type": "STRING"}},
            "questions": {"type": "ARRAY", "items": _QUESTION_SCHEMA},
        },
        "required": ["summary", "objectives", "concepts", "key_terms", "recap", "questions"],
    }

    def build(data):
        concepts = [
            {"title": str(c["title"]).strip(), "explanation": str(c["explanation"]).strip(),
             "example": str(c.get("example") or "").strip()}
            for c in data.get("concepts", [])
            if isinstance(c, dict) and c.get("title") and c.get("explanation")
        ]
        titles = [c["title"] for c in concepts]
        questions = []
        for q in data.get("questions", [])[:8]:
            cleaned = _clean_question(q, title, titles)
            if cleaned:
                questions.append(cleaned)
        summary = str(data.get("summary") or "").strip()
        if len(questions) < 6 or not summary or not concepts:
            raise ValueError("incomplete learning kit")
        key_terms = [{"term": str(t["term"]).strip(), "definition": str(t.get("definition") or "").strip()}
                     for t in data.get("key_terms", []) if isinstance(t, dict) and t.get("term")]
        return {"summary": summary, "objectives": _strings(data.get("objectives")), "concepts": concepts,
                "key_terms": key_terms, "recap": _strings(data.get("recap")), "questions": questions}

    return _generate(prompt, schema, 0.55, build)


# ---------- coaching ----------

def make_coach_feedback(title, notes, questions, submitted, score):
    attempt, topic_stats = [], {}
    for item in submitted:
        q = questions[item["index"]]
        sel = item["selected"]
        topic = q.get("topic", title)
        attempt.append({
            "question": q["question"], "topic": topic,
            "selected_answer": q["options"][sel] if sel in range(len(q["options"])) else "Not answered",
            "correct_answer": q["options"][q["answer"]], "is_correct": item["correct"],
            "explanation": q.get("explanation", ""),
        })
        stat = topic_stats.setdefault(topic, {"correct": 0, "total": 0})
        stat["correct"] += int(item["correct"])
        stat["total"] += 1
    choices = list(topic_stats)

    prompt = f"""You are SmartStudy AI's adaptive learning coach.
Use only the supplied lesson and quiz attempt. Do not claim long-term patterns from one attempt.
Return JSON with:
- feedback: encouraging assessment in 2-4 sentences grounded in this attempt
- recommendations: 2-4 specific, actionable study steps
- practice_questions: exactly 3 multiple-choice questions (4 options each), focused on missed concepts.
  Each question's topic must be copied exactly from one of the topics in the topic performance data.
If nothing was missed, make the practice more challenging. Do not invent facts beyond the lesson.

Lesson title: {title}
Lesson notes:
{notes[:12000]}
Score: {score}/{len(questions)}
Topic performance: {json.dumps(topic_stats)}
Attempt details: {json.dumps(attempt)}
"""
    schema = {
        "type": "OBJECT",
        "properties": {
            "feedback": {"type": "STRING"},
            "recommendations": {"type": "ARRAY", "items": {"type": "STRING"}},
            "practice_questions": {"type": "ARRAY", "items": _QUESTION_SCHEMA},
        },
        "required": ["feedback", "recommendations", "practice_questions"],
    }

    def build(data):
        practice = [c for q in data.get("practice_questions", [])[:3] if (c := _clean_question(q, title, choices))]
        feedback = str(data.get("feedback") or "").strip()
        if not feedback or len(practice) < 2:
            raise ValueError("incomplete coaching feedback")
        return {"feedback": feedback, "recommendations": _strings(data.get("recommendations"))[:4],
                "practice_questions": practice}

    return _generate(prompt, schema, 0.3, build)


def generate_topic_practice(topic, notes, count):
    """Generate only the extra questions needed to reach a full focused-practice set."""
    prompt = f"""Create exactly {count} additional college-level multiple-choice practice questions for the topic: {topic!r}.
Use ONLY the lesson notes below. Test understanding and application rather than repeating wording.
Each question needs exactly four plausible options, one correct zero-based answer index, a short explanation,
and `topic` set to exactly {topic!r}.

LESSON NOTES:
{notes[:12000]}
"""
    schema = {"type": "OBJECT", "properties": {"questions": {"type": "ARRAY", "items": _QUESTION_SCHEMA}},
              "required": ["questions"]}

    def build(data):
        out = [c for q in data.get("questions", [])[:count] if (c := _clean_question(q, topic, [topic]))]
        if len(out) < count:
            raise ValueError("too few valid practice questions")
        return out

    return _generate(prompt, schema, 0.35, build)


# ---------- syllabus ----------

def extract_syllabus_outline(text):
    prompt = ("Extract a college syllabus outline from the supplied text. Return JSON with title (string) and "
              "units (array of objects with title and topics strings). Preserve original unit names and order. "
              "Do not invent units. Keep topics concise. If unclear, say so in topics.\n\nSYLLABUS TEXT:\n" + text[:35000])
    schema = {"type": "OBJECT", "properties": {
        "title": {"type": "STRING"},
        "units": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "title": {"type": "STRING"}, "topics": {"type": "STRING"}}, "required": ["title", "topics"]}}},
        "required": ["title", "units"]}

    def build(data):
        units = [{"title": str(u["title"]).strip()[:180], "topics": str(u.get("topics") or "").strip()[:1200]}
                 for u in data.get("units", []) if isinstance(u, dict) and str(u.get("title", "")).strip()]
        if not units:
            raise ValueError("no clear units")
        return {"title": str(data.get("title") or "Uploaded syllabus").strip()[:180], "units": units[:40]}

    return _generate(prompt, schema, 0.1, build)
