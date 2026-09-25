"""Human-approved Practical AI Foundations Concept Graph V1.

This is authored content, not a generated curriculum.  The manifest is
deliberately limited to the fields already supported by AIL.1A and AIL.5.
"""

from __future__ import annotations

from typing import Any

from app.academy_curriculum import FOUNDATIONS_PROGRAM
from app.db.enums import ConceptKind, ConceptLevel, ConceptRelationType, GradingMode, LearningItemType
from app.services.independence_policy import validate_evidence_requirements


def _leg(evidence_type: str, *, grader_in: list[str] | None = None) -> dict[str, Any]:
    leg: dict[str, Any] = {"evidence_type": evidence_type}
    if grader_in is not None:
        leg["grader_in"] = grader_in
    return leg


DET = [GradingMode.DETERMINISTIC.value]
JUDGED = [GradingMode.AI_RUBRIC.value, GradingMode.HUMAN.value]


def _item(item_type: str, title: str, body_md: str, *, grading_mode: str = "deterministic", est_minutes: int = 15) -> dict[str, Any]:
    return {
        "item_type": item_type,
        "title": title,
        "body_md": body_md,
        "grading_mode": grading_mode,
        "reviewed": True,
        "est_minutes": est_minutes,
    }


def _concept(
    slug: str,
    name: str,
    level: str,
    kind: str,
    plain: str,
    technical: str,
    examples: str,
    evidence: dict[str, Any],
    *,
    core: bool = True,
    prerequisites: tuple[str, ...] = (),
    items: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": name,
        "level": level,
        "kind": kind,
        "is_core": core,
        "plain_definition": plain,
        "technical_explanation": technical,
        "examples_md": examples,
        "evidence_requirements": evidence,
        "prerequisites": list(prerequisites),
        "related": [],
        "learning_items": list(items),
    }


_knowledge = lambda: {"requires_all": [_leg("knowledge_check", grader_in=DET)]}
# The approved authoring vocabulary says “scenario”; the current evidence
# enum has no scenario value, so scenario work is represented by the existing
# deterministic interpretation contract.
_scenario = lambda: {"requires_all": [_leg("interpretation", grader_in=DET)]}
_modification = lambda: {"requires_all": [_leg("modification", grader_in=DET)]}
_lab = lambda: {"requires_all": [_leg("lab", grader_in=DET)]}
_project = lambda: {"requires_all": [_leg("project_assessment", grader_in=DET)]}


FOUNDATIONS_CONCEPT_MANIFEST: list[dict[str, Any]] = [
    _concept("what-ai-is-and-isnt", "What AI Is and Isn't", "foundational", "definitional",
        "Artificial intelligence is a broad category of computer systems designed to perform tasks that normally require aspects of human intelligence, such as recognizing patterns, generating language, making predictions, or selecting actions. AI systems do not automatically possess human understanding, judgment, consciousness, or guaranteed knowledge of truth.",
        "Modern AI includes multiple approaches such as machine learning and generative models. Their behavior comes from algorithms, models, training data, inputs, and system design. A useful way to evaluate an AI system is by its capabilities, limitations, inputs, outputs, and evidence of performance rather than treating “AI” as a single technology.",
        "Chatbots, recommendation systems, image generation, fraud detection, transcription.\n\nCommon contrast to teach: AI capability ≠ human understanding.",
        {"requires_all": [_leg("knowledge_check", grader_in=DET), _leg("explain_back", grader_in=JUDGED)]},
        items=(_item("check_question", "Explain AI in your own words", "Explain AI in your own words and distinguish realistic AI capabilities from exaggerated claims."),
               _item("scenario", "AI capability versus human understanding", "Classify claims that confuse AI capability with human understanding."))),
    _concept("generative-ai-llms", "Generative AI & LLMs", "foundational", "mechanism",
        "Generative AI creates new content from patterns learned from data. Large language models are generative models specialized in processing and producing language and related representations.",
        "An LLM processes tokens and predicts likely continuations based on learned statistical patterns and the context supplied during inference. This produces remarkably useful language behavior but does not make every generated statement factual.",
        "Drafting an email, explaining code, summarizing documents, generating structured JSON, answering questions.",
        {"requires_all": [_leg("knowledge_check", grader_in=DET), _leg("interpretation", grader_in=DET)]},
        prerequisites=("what-ai-is-and-isnt",),
        items=(_item("check_question", "Generative AI and LLMs", "Explain how generative AI and LLMs relate, and why useful language behavior does not guarantee factuality."),)),
    _concept("models-providers-applications", "Models vs. Providers vs. Applications", "foundational", "architectural",
        "A model is the AI capability itself. A provider is an organization or service exposing models. An application is a product using one or more models to solve user problems.",
        "Changing the model does not necessarily mean changing the whole application, and an application can orchestrate multiple models or services.",
        "Model: the AI capability. Provider: the organization or service exposing it. Application: the product using it.",
        _scenario(), prerequisites=("generative-ai-llms",),
        items=(_item("scenario", "Classify model, provider, and application", "Classify each part of an AI product as a model, provider, or application."),)),
    _concept("tokens", "Tokens", "foundational", "mechanism",
        "Tokens are units into which model input and output are represented for processing. They affect context usage, model limits, latency, and often cost.",
        "A token is not simply equivalent to a word. Long prompts and documents consume more model capacity because their text is represented by more tokens.",
        "Token counts used to reason about prompt length, context capacity, latency, and cost.",
        {"requires_all": [_leg("knowledge_check", grader_in=DET), _leg("interpretation", grader_in=DET)]},
        prerequisites=("generative-ai-llms",),
        items=(_item("check_question", "Token and cost interpretation", "Interpret a simple token-count and relative-cost example; do not assume a token equals a word."),)),
    _concept("context-windows", "Context Windows", "foundational", "mechanism",
        "The context window is the amount of tokenized information a model can consider during an interaction.",
        "Input, conversation history, retrieved material, system instructions, tool results, and output budget compete for finite context. Giving the model everything is not necessarily a sound architecture.",
        "A long conversation, retrieved documents, system instructions, tool results, and an output budget sharing one context window.",
        {"requires_any_of": [[_leg("interpretation", grader_in=DET), _leg("explain_back", grader_in=JUDGED)]]},
        prerequisites=("tokens",),
        items=(_item("scenario", "Finite context scenario", "Explain why giving a model every available document is not necessarily a sound architecture."),)),
    _concept("inference", "Inference", "foundational", "mechanism",
        "Inference is the process of running a trained model on input to generate a prediction or output.",
        "Training adjusts a model using data; inference runs the trained model on supplied input. Between sending a prompt and receiving an answer, the input is represented, processed with the model and context, and decoded into output.",
        "Sending a prompt to a trained language model and receiving a generated answer.",
        _knowledge(), prerequisites=("generative-ai-llms", "tokens"),
        items=(_item("check_question", "Training versus inference", "Distinguish training from inference and describe what happens between a prompt and a response."),)),
    _concept("hallucination-grounding", "Hallucination & Grounding", "foundational", "operational",
        "A hallucination is generated information that appears plausible but is unsupported or incorrect. Grounding gives the model relevant trusted information or evidence against which an answer can be formed or checked.",
        "Grounding can reduce error but does not create an absolute guarantee of correctness. Identify unsupported claims in an AI response and redesign the interaction to provide stronger grounding.",
        "An unsupported claim in an AI response; a response grounded in relevant trusted source material.",
        {"requires_all": [_leg("interpretation", grader_in=DET), _leg("modification", grader_in=DET)]},
        prerequisites=("inference", "context-windows"),
        items=(_item("exercise", "Find unsupported claims", "Identify unsupported claims in an AI response and modify the interaction to provide stronger grounding."),)),
    _concept("evaluating-ai-claims", "Evaluating AI Claims", "foundational", "operational",
        "Evaluating AI claims means judging statements about AI using evidence rather than marketing language or model confidence.",
        "Ask: What was tested? Against what baseline? On what data? What does the metric measure? Can the result generalize to this use case?",
        "A benchmark claim examined by checking its test data, baseline, metric, and relevance to the intended use case.",
        {"requires_any_of": [[_leg("interpretation", grader_in=DET), _leg("interpretation", grader_in=DET)]]},
        prerequisites=("what-ai-is-and-isnt", "hallucination-grounding"),
        items=(_item("scenario", "Evaluate an AI claim", "Evaluate an AI claim by identifying the test, baseline, data, metric, and generalization question."),)),
    _concept("privacy-responsible-ai-use", "Privacy & Responsible AI Use", "foundational", "operational",
        "Responsible AI use considers information sensitivity before submitting it to AI systems and includes privacy, security, human oversight, appropriate verification, and awareness of consequences.",
        "Classify inputs as appropriate, sensitive, or requiring organizational approval before using them with an AI system.",
        "An appropriate input, a sensitive input, and an input requiring organizational approval.",
        _scenario(), prerequisites=("what-ai-is-and-isnt",),
        items=(_item("scenario", "Classify AI inputs", "Classify several inputs as appropriate, sensitive, or requiring organizational approval."),)),
    _concept("prompt-structure", "Prompt Structure", "foundational", "operational",
        "A practical prompt structure is Goal → Context → Instructions → Constraints → Input → Expected output.",
        "Prompting is specification and communication, not discovering magic words. Each part makes the requested task and output more explicit.",
        "Goal → Context → Instructions → Constraints → Input → Expected output.",
        _modification(), prerequisites=("context-windows",),
        items=(_item("exercise", "Improve a weak prompt", "Improve a weak prompt using Goal, Context, Instructions, Constraints, Input, and Expected output."),)),
    _concept("system-instructions", "System Instructions", "practitioner", "architectural",
        "System instructions are stable application-level behavioral instructions, distinct from an individual user request.",
        "Applications establish stable behavioral instructions separately from user tasks, but system instructions are not an infallible security boundary.",
        "Application-level instructions establishing stable behavior and a user request supplying a specific task.",
        {"requires_all": [_leg("interpretation", grader_in=DET), _leg("modification", grader_in=DET)]},
        prerequisites=("prompt-structure",),
        items=(_item("scenario", "System instruction boundary", "Distinguish an application-level system instruction from a user request and identify the security-boundary limitation."),)),
    _concept("few-shot-prompting", "Few-Shot Prompting", "practitioner", "operational",
        "Few-shot prompting supplies examples demonstrating desired behavior or output.",
        "Examples can clarify ambiguous requirements, while bad examples can propagate bad behavior.",
        "A prompt containing examples of the desired classification or output format.",
        _modification(), core=False, prerequisites=("prompt-structure",),
        items=(_item("exercise", "Add useful examples", "Modify a prompt with examples that clarify the desired behavior without propagating bad behavior."),)),
    _concept("json-basics", "JSON Basics", "foundational", "definitional",
        "JSON uses objects, arrays, strings, numbers, booleans, null, keys and values, nesting, and valid syntax.",
        "JSON literacy is sufficient to understand AI APIs, structured output, and tool calls; becoming a programmer is not required.",
        "An object with keys and values, an array, nested data, and valid JSON literals.",
        {"requires_all": [_leg("knowledge_check", grader_in=DET), _leg("modification", grader_in=DET)]},
        items=(_item("check_question", "JSON syntax check", "Identify JSON objects, arrays, values, nesting, and valid syntax."), _item("exercise", "Repair malformed JSON", "Repair malformed JSON without changing the intended data."))),
    _concept("structured-output", "Structured Output", "practitioner", "operational",
        "Structured output means requesting or enforcing output that conforms to a defined machine-readable structure.",
        "Software needs predictable structures rather than free-form prose, and validation remains important even when a structure is requested.",
        "A model response transformed into a defined JSON contract and validated before application use.",
        _lab(), prerequisites=("json-basics", "prompt-structure"),
        items=(_item("lab", "Define a structured output contract", "Transform an unstructured model response into a defined JSON contract and validate it."),)),
    _concept("iterating-debugging-ai-outputs", "Iterating & Debugging AI Outputs", "practitioner", "operational",
        "Debugging AI outputs is a systematic diagnosis of whether the instruction was ambiguous, context missing, format unclear, knowledge unavailable, or validation absent.",
        "Diagnose ambiguity, missing context, unclear format, unavailable information, and application validation failures before changing the model or prompt blindly.",
        "A weak output diagnosed by checking instruction, context, format, knowledge, and validation in order.",
        {"requires_all": [_leg("debugging", grader_in=DET)]},
        prerequisites=("prompt-structure", "hallucination-grounding"),
        items=(_item("exercise", "Debug an AI output", "Diagnose an AI output using the ambiguity, context, format, knowledge, and validation questions."),)),
    _concept("model-pricing-token-economics", "Model Pricing & Token Economics", "practitioner", "operational",
        "AI usage has economic consequences based on model and provider pricing and usage dimensions, and application design influences cost.",
        "Compare hypothetical workloads and calculate or interpret relative costs without hard-coding current vendor prices into the concept definition.",
        "Two hypothetical workloads compared by input tokens, output tokens, model choice, and application design.",
        {"requires_all": [_leg("interpretation", grader_in=DET)]},
        prerequisites=("tokens",),
        items=(_item("exercise", "Compare hypothetical AI costs", "Compare hypothetical workloads and calculate or interpret relative costs; do not use current vendor prices."),)),
    _concept("choosing-a-model", "Choosing a Model", "practitioner", "operational",
        "Choosing a model is a tradeoff among task quality, latency, cost, context requirements, modalities, reliability, tool capabilities, and operational constraints.",
        "There is no universal best model. Select and justify a model for a defined application using the relevant tradeoffs.",
        "A model-selection decision justified by task quality, latency, cost, context, modality, reliability, tools, and operational constraints.",
        _scenario(), prerequisites=("models-providers-applications", "model-pricing-token-economics"),
        items=(_item("scenario", "Select and justify a model", "Select and justify a model for a defined application using explicit tradeoffs."),)),
    _concept("evaluation", "Evaluation", "practitioner", "operational",
        "Evaluation is the systematic measurement of whether an AI system performs its intended task sufficiently well.",
        "Use test cases, expected behavior, success criteria, failure categories, repeatability, and human review where appropriate. Design a small evaluation set for an AI task.",
        "A small evaluation set with test cases, expected behavior, success criteria, and failure categories.",
        {"requires_all": [_leg("lab", grader_in=DET), _leg("project_assessment", grader_in=DET)]},
        prerequisites=("evaluating-ai-claims", "hallucination-grounding"),
        items=(_item("lab", "Design a small evaluation set", "Design a small evaluation set for an AI task with test cases, expected behavior, success criteria, and failure categories."),)),
    _concept("how-apps-call-models", "How Apps Call Models", "practitioner", "architectural",
        "An application calls a model through a conceptual request lifecycle: Application → API/client → provider → model inference → response → application validation/use.",
        "The lifecycle includes authentication conceptually, request parameters, error handling, latency, and response parsing without requiring provider-specific API memorization.",
        "Application → API/client → provider → model inference → response → application validation/use.",
        {"requires_all": [_leg("explain_back", grader_in=JUDGED), _leg("lab", grader_in=DET)]},
        prerequisites=("models-providers-applications", "json-basics"),
        items=(_item("lab", "Trace a model request lifecycle", "Explain and diagram the request lifecycle, including validation and response parsing."),)),
    _concept("prompt-templates", "Prompt Templates", "practitioner", "operational",
        "A prompt template combines stable instructions with variable application data.",
        "Separate static instructions from dynamic input and handle missing or untrusted values safely.",
        "A stable prompt template populated with variable application data and safe handling for missing or untrusted values.",
        {"requires_all": [_leg("modification", grader_in=DET), _leg("lab", grader_in=DET)]},
        core=False, prerequisites=("prompt-structure", "how-apps-call-models"),
        items=(_item("lab", "Build a safe prompt template", "Separate stable instructions from variable data and handle missing or untrusted values safely."),)),
    _concept("conversation-memory", "Conversation Memory", "practitioner", "architectural",
        "The model does not inherently remember every previous interaction; applications create memory behavior through history, stored state, summaries, retrieval, or other mechanisms.",
        "Conversation memory is application architecture that selects what prior information is stored, summarized, retrieved, and supplied to later model calls.",
        "Conversation history, stored state, summaries, and retrieval used to create memory behavior.",
        _scenario(), core=False, prerequisites=("context-windows",),
        items=(_item("scenario", "Design conversation memory", "Explain which application mechanism supplies prior information in a conversation-memory design."),)),
    _concept("embeddings", "Embeddings", "practitioner", "mechanism",
        "Embeddings represent content as numeric vectors whose relationships can support similarity-based operations.",
        "Use embeddings conceptually for semantic search, document retrieval, and similarity matching rather than starting with vector mathematics.",
        "Semantic search, document retrieval, similarity matching.",
        {"requires_all": [_leg("interpretation", grader_in=DET), _leg("lab", grader_in=DET)]},
        core=False, prerequisites=("generative-ai-llms",),
        items=(_item("lab", "Use embeddings for similarity", "Interpret a simple similarity result and use it to support semantic search or retrieval."),)),
    _concept("rag", "Retrieval-Augmented Generation (RAG)", "practitioner", "architectural",
        "RAG retrieves relevant external information and supplies it as context for generation.",
        "The pipeline is User request → retrieval → relevant context → model → grounded response. RAG can fail because of poor retrieval, bad source material, insufficient context, or generation errors.",
        "User request → retrieval → relevant context → model → grounded response.",
        {"requires_all": [_leg("lab", grader_in=DET), _leg("debugging", grader_in=DET)]},
        prerequisites=("embeddings", "hallucination-grounding", "context-windows"),
        items=(_item("lab", "Build and debug a RAG pipeline", "Trace retrieval to grounded response and diagnose poor retrieval, bad sources, insufficient context, or generation errors."),)),
    _concept("tool-calling", "Tool Calling", "practitioner", "architectural",
        "Tool calling lets a model request that an application perform a defined operation using structured arguments.",
        "The model proposes or requests; the application validates and authorizes; the tool executes; the result returns. The model does not possess unrestricted external authority.",
        "Model request → application validation/authorization → tool execution → result.",
        {"requires_all": [_leg("interpretation", grader_in=DET), _leg("lab", grader_in=DET)]},
        prerequisites=("structured-output", "how-apps-call-models"),
        items=(_item("lab", "Design an authorized tool call", "Design a tool call showing model request, application validation/authorization, execution, and returned result."),)),
    _concept("agents", "Agents", "practitioner", "architectural",
        "An AI agent is a system in which a model participates in deciding or coordinating actions toward a goal using context, state, tools, and application-defined controls.",
        "Agent does not imply autonomy without boundaries. The application defines context, state, tools, authorization, and control limits.",
        "A bounded system using a model, context, state, tools, and application-defined controls toward a goal.",
        {"requires_all": [_leg("explain_back", grader_in=JUDGED), _leg("interpretation", grader_in=DET)]},
        prerequisites=("tool-calling", "conversation-memory"),
        items=(_item("scenario", "Bound an agent", "Explain the controls and boundaries in an agent architecture and why agent does not mean unrestricted autonomy."),)),
    _concept("workflows", "Workflows", "practitioner", "architectural",
        "A workflow organizes tasks or steps into a defined execution process. AI may participate in some steps without controlling the entire workflow.",
        "Distinguish deterministic orchestration from model-driven decisions when designing a small workflow.",
        "A defined sequence of tasks with AI participating in selected steps and deterministic orchestration controlling the process.",
        _lab(), prerequisites=("agents", "tool-calling"),
        items=(_item("lab", "Design a small workflow", "Design a small workflow and distinguish deterministic orchestration from model-driven decisions."),)),
    _concept("application-evaluation", "Application Evaluation", "practitioner", "operational",
        "Application evaluation measures the whole AI application rather than an isolated model response.",
        "Consider retrieval, prompts, model choice, tools, latency, cost, failures, safety, user experience, and end-to-end task success.",
        "An end-to-end evaluation covering retrieval, prompts, model choice, tools, latency, cost, failures, safety, user experience, and task success.",
        _project(), prerequisites=("evaluation", "how-apps-call-models"),
        items=(_item("lab", "Evaluate an AI application", "Design an end-to-end application evaluation that covers the application factors listed in this concept."),)),
    _concept("problem-framing-requirements", "Problem Framing & Requirements", "practitioner", "operational",
        "Problem framing begins with the problem rather than an AI technology.",
        "Define User → Problem → Desired outcome → Inputs → Constraints → Risks → Success criteria → Why AI is/isn't appropriate. Frame an AI problem and design a small solution using concepts from the program.",
        "User → Problem → Desired outcome → Inputs → Constraints → Risks → Success criteria → Why AI is/isn't appropriate.",
        _project(), prerequisites=("evaluation", "application-evaluation"),
        items=(_item("lab", "Frame an AI problem", "Frame an AI problem using user, problem, outcome, inputs, constraints, risks, success criteria, and why AI is or is not appropriate."),)),
]

# Evaluation is intentionally a related association, not a hard prerequisite:
# Choosing a Model appears earlier in the frozen 30-day schedule.
for _entry in FOUNDATIONS_CONCEPT_MANIFEST:
    if _entry["slug"] == "choosing-a-model":
        _entry["related"].append({"slug": "evaluation", "label": "associated_with"})


CANONICAL_FOUNDATIONS_SLUGS = tuple(entry["slug"] for entry in FOUNDATIONS_CONCEPT_MANIFEST)


def validate_practical_ai_foundations_manifest() -> None:
    """Validate all static safety gates before any database mutation."""
    expected = {
        "what-ai-is-and-isnt", "generative-ai-llms", "models-providers-applications", "tokens", "context-windows", "inference", "hallucination-grounding", "evaluating-ai-claims", "privacy-responsible-ai-use", "prompt-structure", "system-instructions", "few-shot-prompting", "json-basics", "structured-output", "iterating-debugging-ai-outputs", "model-pricing-token-economics", "choosing-a-model", "evaluation", "how-apps-call-models", "prompt-templates", "conversation-memory", "embeddings", "rag", "tool-calling", "agents", "workflows", "application-evaluation", "problem-framing-requirements",
    }
    actual = [entry["slug"] for entry in FOUNDATIONS_CONCEPT_MANIFEST]
    if len(actual) != 28 or len(set(actual)) != 28 or set(actual) != expected:
        raise ValueError("Practical AI Foundations manifest must contain exactly the 28 canonical slugs")
    by_slug = {entry["slug"]: entry for entry in FOUNDATIONS_CONCEPT_MANIFEST}
    for entry in FOUNDATIONS_CONCEPT_MANIFEST:
        ConceptLevel(entry["level"])
        ConceptKind(entry["kind"])
        validate_evidence_requirements(entry["evidence_requirements"])
        for item in entry["learning_items"]:
            LearningItemType(item["item_type"])
            if item.get("grading_mode"):
                GradingMode(item["grading_mode"])
        for prerequisite in entry["prerequisites"]:
            if prerequisite not in by_slug or prerequisite == entry["slug"]:
                raise ValueError(f"Invalid prerequisite endpoint: {prerequisite} -> {entry['slug']}")
        for related in entry["related"]:
            target = related["slug"]
            if target not in by_slug or target == entry["slug"]:
                raise ValueError(f"Invalid related endpoint: {entry['slug']} -> {target}")

    edges = {slug: set() for slug in actual}
    for entry in FOUNDATIONS_CONCEPT_MANIFEST:
        for prerequisite in entry["prerequisites"]:
            edges[prerequisite].add(entry["slug"])
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(slug: str) -> None:
        if slug in visiting:
            raise ValueError(f"Prerequisite cycle detected at {slug}")
        if slug in visited:
            return
        visiting.add(slug)
        for dependent in edges[slug]:
            visit(dependent)
        visiting.remove(slug)
        visited.add(slug)

    for slug in actual:
        visit(slug)

    # Validate compatibility with the frozen Academy ordering.
    day_by_slug: dict[str, int] = {}
    day = 1
    for _module_key, _title, slugs in FOUNDATIONS_PROGRAM["modules"]:
        for slug in slugs:
            day_by_slug.setdefault(slug, day)
            day = min(30, day + 1)
    for entry in FOUNDATIONS_CONCEPT_MANIFEST:
        for prerequisite in entry["prerequisites"]:
            if day_by_slug[prerequisite] > day_by_slug[entry["slug"]]:
                raise ValueError(f"Prerequisite is scheduled after dependent: {prerequisite} -> {entry['slug']}")


validate_practical_ai_foundations_manifest()
