"""
What autonomy will and won't answer for you.

A permission gate asks about risk ("run this command?"), and the autonomy level
says how much risk the human already signed off on — so yolo can press yes. A
multiple-choice question asks about the work ("which approach?"), and no level
implies an answer to that. Those wait for a human at every level, including
yolo, and reach Slack even on a session that otherwise answers itself.
"""

from server import autonomy


def gate(*labels, **extra):
    p = {"question": "Do you want to proceed?", "context": "",
         "options": [{"num": i + 1, "label": l} for i, l in enumerate(labels)]}
    p.update(extra)
    return p


# --- permission gates: yolo answers them ------------------------------------

def test_claude_code_gate_is_not_a_choice():
    p = gate("Yes", "Yes, and don't ask again for Bash commands",
             "No, and tell Claude what to do differently (esc)")
    assert not autonomy.is_choice(p)
    assert autonomy.decide("yolo", p) == 1


def test_opencode_gate_is_not_a_choice():
    p = gate("Allow once", "Allow always", "Reject", stage="permission")
    assert not autonomy.is_choice(p)
    assert autonomy.decide("yolo", p) == 1


def test_agy_gate_is_not_a_choice():
    p = gate("✓ Approve (ctrl+k)", "⚙ Manage (alt+j)", "✕ Reject (esc)", agy=True)
    assert not autonomy.is_choice(p)
    assert autonomy.decide("yolo", p) == 1


def test_trust_prompt_still_answered():
    p = gate("Yes, proceed", "No, cancel")
    assert autonomy.decide("yolo", p) == 1


# --- multiple choice: nobody auto-answers -----------------------------------

def test_numbered_choices_are_left_to_the_human():
    p = gate("Postgres", "MySQL", "SQLite", "DynamoDB")
    assert autonomy.is_choice(p)
    assert autonomy.decide("yolo", p) is None
    assert autonomy.decide("auto-safe", p) is None


def test_choice_survives_a_safe_sounding_blob():
    # auto-safe used to approve anything that read read-only; a question about
    # which file to read is still a question.
    p = gate("Read the parser", "Read the runner", "Read both")
    p["question"] = "Which file should I read first?"
    assert autonomy.decide("auto-safe", p) is None


def test_choice_with_one_yes_looking_row_is_still_a_choice():
    # No way to say no => not a permission gate, whatever the first row says.
    p = gate("Yes, use Redis", "Use Memcached", "Use in-process cache")
    assert autonomy.is_choice(p)
    assert autonomy.decide("yolo", p) is None


def test_opencode_ask_dialog_is_a_choice():
    p = gate("Allow it", "Reject it", stage="ask")
    assert autonomy.is_choice(p)
    assert autonomy.decide("yolo", p) is None


def test_free_text_row_is_a_choice():
    p = gate("Allow", "Reject", "Type your own answer", custom=3)
    assert autonomy.is_choice(p)


def test_checkbox_widget_is_a_choice():
    # A digit toggles a checkbox here — pressing one never submits an answer.
    p = gate("Yes", "No", multi=True)
    assert autonomy.is_choice(p)
    assert autonomy.decide("yolo", p) is None


def test_manual_answers_nothing():
    assert autonomy.decide("manual", gate("Yes", "No")) is None
