"""AIL.5D.5 — the authored Educational Lab Kits (curriculum data, not code paths).

Kits live here, in the repository, so they are reviewed like all authored curriculum, are immutable at runtime (no learner
and no API can change them) and need no table of their own. A Practice Instance freezes the scenario it was created from, so
a later kit revision never alters a learner's work in progress.

The scenarios are drawn from the authored Day 4, Day 5 and Day 9 labs. They do not rewrite the lab: they take the lab's own
prompts, documents and variations and let the learner run them on the governed engine with ONE controlled thing to change.
"""

from __future__ import annotations

from typing import Dict, Optional

from app import academy_lab_kit as K

AGENT_PROMPT = (
    "You are a general-purpose assistant used in a learning lab. Answer the request as asked. "
    "Do not add facts you are not confident about."
)


def _agent(key: str, name: str) -> dict:
    return {"key": key, "name": name, "role": "academy_lab", "prompt": AGENT_PROMPT}


def _opt(key: str, label: str, text: str) -> dict:
    return {"key": key, "label": label, "text": text}


# -- Day 4: AI Behavior Lab ---------------------------------------------------------------------------------------------------

_D4_PROMPTS = [
    _opt("speed-of-light", "A - Basic success", "What is the speed of light in a vacuum, in meters per second?"),
    _opt("pulitzer", "B - Specific fact", "Who won the Pulitzer Prize for Fiction in 1987? Provide the full name of the winner and the title of the winning book."),
    _opt("pulitzer-biography", "B - Biography variation", "Who won the Pulitzer Prize for Fiction in 1987? Make sure to include the author's complete biography including their early life and major influences."),
    _opt("good-teacher", "C - Nondeterminism", "In exactly three sentences, describe what makes a good teacher."),
    _opt("widgets-revenue", "D1 - Fictional fact", "What was the exact sales revenue of Widgets Corp in Q3 2024? Provide the figure in USD."),
    _opt("nature-citation", "D2 - Citation", "What is the citation for the foundational academic paper on prompt engineering published in Nature in 2019?"),
    _opt("council-meeting", "D3 - Edge of knowledge", "What happened in the local Cypress, Texas city council meeting last Tuesday?"),
    _opt("attention-ungrounded", "E - Ungrounded", 'What are the main arguments in the paper "Attention Is All You Need"?'),
    _opt("attention-grounded", "E - Grounded", (
        "I am going to give you the abstract of a paper. Then I want you to summarize the main arguments based ONLY on what I "
        "provide. Do not add anything from memory.\n\nAbstract: \"The dominant sequence transduction models are based on complex "
        "recurrent or convolutional neural networks that include an encoder and a decoder. The best performing models also "
        "connect the encoder and decoder through an attention mechanism. We propose a new simple network architecture, the "
        "Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely. Experiments on "
        "two machine translation tasks show these models to be superior in quality while being more parallelizable and requiring "
        "significantly less time to train.\"\n\nSummarize the main arguments based only on the abstract above.")),
    _opt("sp500", "Debugging - S&P 500", "List the last 10 companies to join the S&P 500 index, with the dates they joined."),
]

_D4_REVEAL = (
    "What to notice: Experiment A is easy because the fact is stable and widely repeated. Experiment B asks for a specific fact "
    "where plausible-sounding errors are common (the 1987 winner was Peter Taylor, \"A Summons to Memphis\"); the biography "
    "variation pushes the model past what it can reliably retrieve. Fictional-company, fabricated-citation and last-Tuesday "
    "prompts have no source to retrieve, so any specific answer is invented. Grounding (Experiment E) ties the answer to text "
    "you supplied."
)

DAY4_KIT = {
    "kit_key": "day4-ai-behavior-lab",
    "lab_kit": {
        "key": "day4-ai-behavior-lab", "version": 1, "title": "AI Behavior Lab", "agent": _agent("academy-lab-day4", "Academy Lab - AI Behavior"),
        "scenarios": [
            {
                "key": "succeed-and-fail", "title": "Make the model succeed - then fail", "mode": "guided",
                "objective_md": "Cause a model to succeed on a well-specified task and fail on a poorly-specified one, and explain the mechanism behind each.",
                "instructions_md": ("Predict first. Then run one prompt, observe the exact output, change **which prompt you run** (the only variable), "
                                    "run again, and compare the two outputs. Explain why each succeeded or failed."),
                "prompt_template": "{prompt}",
                "variables": [{"key": "prompt", "label": "Prompt to run", "kind": "choice", "default": "speed-of-light", "options": _D4_PROMPTS}],
                "predictions": [
                    {"id": "capital", "text": "If I ask the model 'What is the capital of France?' I predict it will ___."},
                    {"id": "unknown", "text": "If I ask the model to name a famous scientist who published a paper in 2024 without telling it any names, I predict ___."},
                    {"id": "nondeterminism", "text": "If I ask the model the same creative writing prompt five times, I predict the outputs will ___."},
                    {"id": "grounding", "text": "If I add the instruction 'You must only use information from this document:' followed by a real document, I predict the model will ___."},
                ],
                "requirements": {"min_runs": 2, "require_change": True, "require_comparison": True, "require_observation": True, "require_reflection": True},
                "limits": {"max_runs": 6},
                "observation_prompt_md": "Record the exact outputs. Was each correct? Label anything the model could not have retrieved from a reliable source.",
                "comparison_prompt_md": "What differed between the two runs, and what mechanism explains the difference?",
                "reflection_prompt_md": "What was the single most surprising thing you observed, and what does it tell you about when to trust AI output?",
                "reveal_md": _D4_REVEAL,
            },
            {
                "key": "practice-find-the-failure", "title": "Practice: find a prompt that makes it fail", "mode": "independent",
                "objective_md": "On your own, find a prompt in the set where the model produces specific-sounding content it cannot support.",
                "instructions_md": "Run two prompts of your choice. Decide which one is more likely to be fabricated, and why.",
                "prompt_template": "{prompt}",
                "variables": [{"key": "prompt", "label": "Prompt to run", "kind": "choice", "default": "pulitzer", "options": _D4_PROMPTS}],
                "requirements": {"min_runs": 2, "require_change": True, "require_observation": False},
                "limits": {"max_runs": 4},
            },
        ],
    },
}

# -- Day 5: Grounded vs. ungrounded --------------------------------------------------------------------------------------------

_HARBOR_FULL = (
    "Harbor Lights Museum - Visitor Guide. The Harbor Lights Museum opened in 1968 in the town of Seabrook. It is housed in a "
    "former lighthouse keeper's cottage. The museum is open Tuesday to Saturday from 10:00 AM to 4:00 PM and is closed on "
    "Sundays and Mondays. Admission is 8 dollars for adults and free for children under 12. The museum's most popular exhibit "
    "is the Fresnel lens from the Seabrook Point lighthouse, which was installed in 1891 and retired in 1974. The museum is "
    "run by a volunteer board of nine people and employs two part-time staff."
)
_HARBOR_EDITED = _HARBOR_FULL.replace(" Admission is 8 dollars for adults and free for children under 12.", "")
_GROUND_RULE = ("Answer my question using ONLY the information in this document. If the answer is not in the document, say "
                "\"I cannot answer this from the provided document.\"\n\nDocument:\n")

_D5_QUESTIONS = [
    _opt("q-opened", "Q1 - Direct", "In what year did the museum open?"),
    _opt("q-closed", "Q2 - Direct", "On which days is the museum closed?"),
    _opt("q-lens-age", "Q3 - Synthesis", "For how many years was the Fresnel lens in service at the lighthouse?"),
    _opt("q-admission", "Q4 - Edge (edited document)", "How much is admission for adults?"),
    _opt("q-parking", "Q5 - Unanswerable", "How many parking spaces does the museum have?"),
]

DAY5_KIT = {
    "kit_key": "day5-grounding-lab",
    "lab_kit": {
        "key": "day5-grounding-lab", "version": 1, "title": "Grounded vs. Ungrounded", "agent": _agent("academy-lab-day5", "Academy Lab - Grounding"),
        "scenarios": [
            {
                "key": "grounded-vs-ungrounded", "title": "Same question, with and without a source", "mode": "guided",
                "objective_md": "Run the same question with and without grounding material, compare the accuracy, and find where grounding still fails.",
                "instructions_md": ("Predict first. Run a question with **no document**, then change only the **source** (add the document) and run it again. "
                                    "Try the unanswerable question, then remove the key sentence from the document and see what the model does."),
                "prompt_template": "{source}\n\nQuestion: {question}",
                "variables": [
                    {"key": "source", "label": "Source", "kind": "choice", "default": "none", "options": [
                        _opt("none", "No document (ungrounded)", "(No document is provided.)"),
                        _opt("document", "Full document (grounded)", _GROUND_RULE + _HARBOR_FULL),
                        _opt("document-edited", "Key sentence removed", _GROUND_RULE + _HARBOR_EDITED)]},
                    {"key": "question", "label": "Question", "kind": "choice", "default": "q-opened", "options": _D5_QUESTIONS},
                ],
                "predictions": [
                    {"id": "accuracy", "text": "When I ask about facts that are in a provided document, I predict the grounded model will be ___ accurate versus the ungrounded model."},
                    {"id": "unknown", "text": "\"I predict the model will always correctly report when it doesn't know something.\" TRUE / FALSE / UNCERTAIN — explain."},
                    {"id": "errors", "text": "Even with grounding, I predict there will be at least ___ error(s) in 5 grounded responses."},
                ],
                "requirements": {"min_runs": 2, "require_change": True, "require_comparison": True, "require_observation": True, "require_reflection": True},
                "limits": {"max_runs": 6},
                "observation_prompt_md": "Score each answer 1, 0 or ? against the document. What did the ungrounded model do on the unanswerable question?",
                "comparison_prompt_md": "Which run was more reliable, and what mechanism explains the difference?",
                "reflection_prompt_md": "Explain grounding in your own words: why it helps, and what it does NOT protect against.",
                "reveal_md": "The museum opened in 1968; it is closed Sundays and Mondays; the lens served 1891-1974 (83 years); admission is 8 dollars (present only in the full document); the document does not give parking.",
            },
            {
                "key": "practice-misleading-source", "title": "Practice: a source that misleads", "mode": "independent",
                "objective_md": "On your own, see whether a source can make an answer worse, not better.",
                "instructions_md": "Run the same question with no source and with the edited document, and decide which answer you would trust.",
                "prompt_template": "{source}\n\nQuestion: {question}",
                "variables": [
                    {"key": "source", "label": "Source", "kind": "choice", "default": "none", "options": [
                        _opt("none", "No document", "(No document is provided.)"),
                        _opt("document-edited", "Document without the answer", _GROUND_RULE + _HARBOR_EDITED)]},
                    {"key": "question", "label": "Question", "kind": "choice", "default": "q-admission", "options": _D5_QUESTIONS[3:]},
                ],
                "requirements": {"min_runs": 2, "require_change": True},
                "limits": {"max_runs": 4},
            },
        ],
    },
}

# -- Day 9: Prompt structure --------------------------------------------------------------------------------------------------

_REVIEWS = [
    _opt("r1", "Case 1 - standard", "The battery lasts two days and the screen is gorgeous. Best phone I have owned."),
    _opt("r2", "Case 2 - standard", "Arrived broken and support never answered my emails."),
    _opt("r3", "Case 3 - standard", "It does what the box says. Nothing more, nothing less."),
    _opt("r4", "Case 4 - edge (mixed)", "Love the camera, but the battery is awful and it overheats."),
    _opt("r5", "Case 5 - adversarial", "Great, another update that breaks everything. Truly wonderful."),
]
_D9_TASK = "Classify the customer review as positive, negative, neutral or mixed."
_D9_VARIANTS = [
    _opt("v1", "Variant 1 - zero-shot", _D9_TASK),
    _opt("v2", "Variant 2 - structured", _D9_TASK + " Return only one of: positive / negative / neutral / mixed - nothing else."),
    _opt("v3", "Variant 3 - few-shot", _D9_TASK + " Return only the label.\n\nExample: \"Fast shipping, but the box was crushed.\" -> mixed\nExample: \"It works.\" -> neutral\nExample: \"Terrible, never again.\" -> negative"),
]

DAY9_KIT = {
    "kit_key": "day9-prompt-structure-lab",
    "lab_kit": {
        "key": "day9-prompt-structure-lab", "version": 1, "title": "Prompt Structure Experiment", "agent": _agent("academy-lab-day9", "Academy Lab - Prompt Structure"),
        "scenarios": [
            {
                "key": "zero-shot-vs-structured", "title": "Does prompt structure change the result?", "mode": "guided",
                "objective_md": "Run a controlled prompt comparison: the same review, three prompt structures; find which works best and why.",
                "instructions_md": "Predict which variant will do best. Run one review with Variant 1, change only the **variant**, run again, and compare. Use the hard cases (4 and 5).",
                "prompt_template": "{variant}\n\nReview: {review}",
                "variables": [
                    {"key": "variant", "label": "Prompt variant", "kind": "choice", "default": "v1", "options": _D9_VARIANTS},
                    {"key": "review", "label": "Review", "kind": "choice", "default": "r4", "options": _REVIEWS},
                ],
                "predictions": [
                    {"id": "best", "text": "Which prompt variant do you think will perform best? Write your prediction before running anything."},
                    {"id": "hardest", "text": "Which cases do you think will be hardest? Why?"},
                ],
                "requirements": {"min_runs": 2, "require_change": True, "require_comparison": True, "require_observation": True, "require_reflection": True},
                "limits": {"max_runs": 6},
                "observation_prompt_md": "Record each output and whether it was correct against your ground truth.",
                "comparison_prompt_md": "What specific change between the variants caused the difference?",
                "reflection_prompt_md": "What is the most important lesson about what makes a prompt work, and what would you tell someone using it in production?",
                "reveal_md": "Case 4 is mixed and case 5 is negative (sarcasm). A bare instruction often returns a sentence; an explicit format and examples make the label reliable.",
            },
            {
                "key": "practice-prompt-variants", "title": "Practice: compare two prompt structures", "mode": "independent",
                "objective_md": "On your own, compare two prompt structures on the hardest review.",
                "instructions_md": "Run the sarcastic review with two different variants and decide which you would ship.",
                "prompt_template": "{variant}\n\nReview: {review}",
                "variables": [
                    {"key": "variant", "label": "Prompt variant", "kind": "choice", "default": "v1", "options": _D9_VARIANTS},
                    {"key": "review", "label": "Review", "kind": "choice", "default": "r5", "options": _REVIEWS},
                ],
                "requirements": {"min_runs": 2, "require_change": True},
                "limits": {"max_runs": 4},
            },
        ],
    },
}

KITS: Dict[str, dict] = {k["kit_key"]: k for k in (DAY4_KIT, DAY5_KIT, DAY9_KIT)}
DAY_KITS = {4: ("day4-ai-behavior-lab", "succeed-and-fail"), 5: ("day5-grounding-lab", "grounded-vs-ungrounded"), 9: ("day9-prompt-structure-lab", "zero-shot-vs-structured")}

for _spec in KITS.values():           # a malformed authored kit must fail at import, never at a learner's request
    K.validate_lab_kit(_spec)


def get_kit(kit_key: str) -> Optional[dict]:
    return KITS.get(kit_key)


def get_scenario(kit_key: str, scenario_key: str) -> Optional[dict]:
    kit = KITS.get(kit_key)
    return K.scenario_of(kit, scenario_key) if kit else None
