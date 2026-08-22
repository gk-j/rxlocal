"""Layer B - the deterministic backstop.

Layer A is the prompt in AGENTS.md. This is what runs when the model ignores
it. `scan()` reads the patient's own words and decides whether an escalation
is mandatory, regardless of what the model passed to log_outcome. Testing
showed the model both under-escalating (logging a reported side effect as a
plain non_adherent) and inventing patient speech, so this layer never trusts
the model's classification - only `raw_patient_text`.

No model call, no config DSL. Microseconds, and it cannot fail on stage.
"""
from __future__ import annotations

import re

# Ordered most severe first. First match wins.
RULES: list[tuple[str, str, str, re.Pattern]] = [
    (
        "emergency", "critical",
        "Patient reported a potential medical emergency",
        re.compile(
            r"\b(911|ambulance|emergency room|\ba\s*&\s*e\b|"
            r"can'?t breathe|cannot breathe|trouble breathing|"
            r"short(ness)? of breath|chest pain|chest tightness|"
            r"passed out|blacked out|unconscious|collapsed|fainted|"
            r"overdose|took too many|double dose|"
            r"suicid\w+|kill myself|end my life|hurt myself|"
            r"throat clos\w+|anaphyla\w+|swollen tongue|"
            r"slurred speech|numb on one side|worst headache)\b", re.I),
    ),
    (
        "adverse_reaction", "high",
        "Patient reported a possible adverse drug reaction",
        re.compile(
            r"\b(rash|hives|welts|itch\w*|blister\w*|peeling|"
            r"swell\w*|swollen|puffy|"
            r"allerg\w+|reaction to|bruis\w+|bleeding|blood in|"
            r"yellow(ing)? (eyes|skin)|jaundice|dark urine|"
            r"muscle pain|muscle ache|very weak|can'?t stand up)\b", re.I),
    ),
    (
        "symptom", "high",
        "Patient reported symptoms or side effects",
        re.compile(
            r"\b(stomach|nausea|nauseous|vomit\w*|throw(ing)? up|diarrhea|"
            r"dizzy|dizziness|light ?headed|headache|migraine|cramp\w*|"
            r"side effect\w*|makes me feel|feel(ing)? (sick|ill|awful|terrible|worse)|"
            r"upset\w*|constipat\w+|heartburn|"
            r"blurry vision|blurred vision|ringing in my ears|"
            r"low (blood )?sugar|hypoglyc\w+|hyperglyc\w+|shaky|sweating|"
            r"insomnia|can'?t sleep|tired all the time|exhausted)\b", re.I),
    ),
    (
        "clinical_question", "medium",
        "Patient asked a question requiring a pharmacist",
        re.compile(
            r"\b(should i (stop|take|keep|switch|double|skip|split|change|cut)|"
            r"can i (stop|take|skip|drink|double|split|cut|mix)|"
            r"is it (safe|ok|okay|bad|fine|alright) to|"
            r"what (dose|dosage|happens if)|how much should|"
            r"instead of|replace it with|different medication|another drug|"
            r"do i (still )?need|why (am i|do i) tak\w+|"
            r"with alcohol|while pregnant|breastfeeding|"
            r"interact\w* with|together with my other)\b", re.I),
    ),
]

SEVERITY_ORDER = ["low", "medium", "high", "critical"]

# Things the agent must never say. Checked separately from the input scan.
ADVICE_LEAK = re.compile(
    r"\b(you should (take|stop|try|switch|reduce|increase|double|halve)|"
    r"i recommend|i'd recommend|i suggest you (take|stop|try)|"
    r"try taking (it )?with|take it with food|take it at night|"
    r"that'?s (normal|fine|nothing to worry|common and harmless)|"
    r"don'?t worry about|it'?s (safe|harmless|not serious)|"
    r"you (don'?t|do not) need to (see|call)|"
    r"no need to (see|call|worry))\b", re.I)

SAFE_DEFLECTION = (
    "That's a question for a pharmacist, and I want to make sure you get a "
    "proper answer rather than a guess from me. I'm flagging this to your "
    "care team now.")


def scan(text: str | None) -> dict:
    """Classify what the patient actually said.

    Returns {escalate, red_flag_type, severity, reason, matched}.
    """
    if not text:
        return {"escalate": False, "red_flag_type": None, "severity": None,
                "reason": None, "matched": None}
    for flag, severity, reason, pattern in RULES:
        m = pattern.search(text)
        if m:
            return {"escalate": True, "red_flag_type": flag,
                    "severity": severity, "reason": reason,
                    "matched": m.group(0)}
    return {"escalate": False, "red_flag_type": None, "severity": None,
            "reason": None, "matched": None}


def enforce(raw_patient_text: str | None,
            model_severity: str | None = None,
            model_red_flag_type: str | None = None,
            model_reason: str | None = None) -> dict:
    """Combine the model's escalation call with the backstop.

    The backstop can only ever ADD an escalation or RAISE its severity, never
    remove or lower one - if the model escalated something the regexes do not
    recognise, that judgement stands.
    """
    found = scan(raw_patient_text)
    model_escalated = bool(model_severity or model_red_flag_type or model_reason)

    if not found["escalate"] and not model_escalated:
        return {"escalate": False, "red_flag_type": None, "severity": None,
                "reason": None, "forced": False, "matched": None}

    if found["escalate"] and not model_escalated:
        # The model missed it. This is the case that matters.
        return {**found, "forced": True}

    if model_escalated and not found["escalate"]:
        return {"escalate": True,
                "red_flag_type": model_red_flag_type or "clinical_question",
                "severity": model_severity or "medium",
                "reason": model_reason or "Escalated by agent",
                "forced": False, "matched": None}

    # Both fired - keep the higher severity.
    ms = model_severity if model_severity in SEVERITY_ORDER else "low"
    higher = max([ms, found["severity"]], key=SEVERITY_ORDER.index)
    return {"escalate": True,
            "red_flag_type": found["red_flag_type"] if higher == found["severity"]
                             else (model_red_flag_type or found["red_flag_type"]),
            "severity": higher,
            "reason": model_reason or found["reason"],
            "forced": higher != ms,
            "matched": found["matched"]}


def check_reply(text: str) -> dict:
    """Last-chance scrub of anything the agent is about to say."""
    m = ADVICE_LEAK.search(text or "")
    if m:
        return {"safe": False, "matched": m.group(0), "text": SAFE_DEFLECTION}
    return {"safe": True, "matched": None, "text": text}
