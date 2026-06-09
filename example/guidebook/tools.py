import inspect
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Annotated, Dict, List, Any, Optional

try:
    from pydantic import Field
except ImportError:
    def Field(**kwargs):
        return kwargs

# === Tool Registry ===

TOOL_REGISTRY = {}
TOOLS_REQUIRE_CONFIRMATION = True

# --- Guidebook JSON (loaded once) ---

from pathlib import Path

_here = Path(__file__).resolve().parent
_candidates = [
    _here / "data" / "cps-ai-guidebook.json",
    _here.parent / "data" / "cps-ai-guidebook.json",
]
DEFAULT_GUIDEBOOK_PATH = next((p for p in _candidates if p.exists()), _candidates[0])

_GUIDEBOOK_DOC = None
_GUIDEBOOK_PATH = Path(os.environ.get("CPS_GUIDEBOOK_JSON_PATH", str(DEFAULT_GUIDEBOOK_PATH)))


def _load_guidebook():
    global _GUIDEBOOK_DOC
    if _GUIDEBOOK_DOC is None:
        with open(_GUIDEBOOK_PATH, "r", encoding="utf-8") as f:
            _GUIDEBOOK_DOC = json.load(f)
    return _GUIDEBOOK_DOC


def _safe_text(value) -> str:
    if isinstance(value, dict):
        return value.get("text") or value.get("name") or str(value)
    return str(value) if value else ""


def register_tool(name, description, system_instructions=None):
    """Registers a callable as an available Claude tool."""
    def decorator(func):
        TOOL_REGISTRY[name] = {
            "function": func,
            "description": description,
            "system_instructions": system_instructions,
        }
        return func
    return decorator


# --- Response helpers ---

def _guidebook_meta() -> Dict[str, Any]:
    doc = _load_guidebook()
    return {
        "source": {
            "dataset": "CPS AI Guidebook",
            "title": doc.get("title") or "Chicago Public Schools AI Guidebook",
            "publisher": doc.get("publisher") or "Chicago Public Schools",
            "copyright": doc.get("copyright"),
            "attribution_required": doc.get("attribution", {}).get("required", True),
        },
        "versioning": {
            "document_id": doc.get("document_id"),
            "semantic_version": doc.get("semantic_version"),
            "released_at": doc.get("released_at"),
        },
    }


def _attribution_line() -> str:
    doc = _load_guidebook()
    version = doc.get("semantic_version") or ""
    return f"Attribution: Chicago Public Schools AI Guidebook v{version} (CPS) · cps.edu/aiguidebook"


def ok(data: Any) -> Dict[str, Any]:
    meta = _guidebook_meta()
    att = _attribution_line()
    if isinstance(data, dict):
        data_out = {**data, "attribution_line": att}
    else:
        data_out = {"result": data, "attribution_line": att}
    return {"ok": True, "data": data_out, "meta": meta}


def err(message: str, *, code: str = "bad_request", details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    meta = _guidebook_meta()
    att = _attribution_line()
    e: Dict[str, Any] = {"message": message, "code": code}
    if details:
        e["details"] = details
    return {"ok": False, "error": e, "data": {"attribution_line": att}, "meta": meta}


# --- Lookup helpers ---

def _find_section(doc, section_id_or_code: str):
    for section in doc.get("sections") or []:
        if section.get("id") == section_id_or_code or section.get("code") == section_id_or_code:
            return section
    return None


def _find_topic(doc, topic_id_or_code: str):
    for section in doc.get("sections") or []:
        for topic in section.get("topics") or []:
            if topic.get("id") == topic_id_or_code or topic.get("code") == topic_id_or_code:
                return {"section": section, "topic": topic}
    return None


def _collect_recommendations(topic_data: dict) -> List[str]:
    """Recursively collect all recommendations from a topic and its subtopics."""
    recs = list(topic_data.get("recommendations") or [])
    for sub in topic_data.get("subtopics") or []:
        recs.extend(sub.get("recommendations") or [])
    return recs


def _collect_all_searchable(doc) -> List[Dict[str, Any]]:
    """Build a flat list of all searchable items for full-text search."""
    items = []
    for section in doc.get("sections") or []:
        items.append({
            "type": "section",
            "id": section["id"],
            "code": section.get("code"),
            "name": section.get("name"),
            "text": section.get("summary") or "",
            "page": section.get("page_start"),
        })
        for topic in section.get("topics") or []:
            text_parts = [topic.get("content") or ""]
            for rec in topic.get("recommendations") or []:
                text_parts.append(rec)
            for sub in topic.get("subtopics") or []:
                text_parts.append(sub.get("content") or "")
                for rec in sub.get("recommendations") or []:
                    text_parts.append(rec)
            # Glossary terms
            for term_entry in topic.get("terms") or []:
                text_parts.append(f"{term_entry['term']}: {term_entry['definition']}")

            items.append({
                "type": "topic",
                "id": topic["id"],
                "code": topic.get("code"),
                "name": topic.get("name"),
                "section_id": section["id"],
                "section_name": section.get("name"),
                "text": " ".join(text_parts),
                "page": topic.get("page"),
            })
    return items


# =============================
# Tools
# =============================

@register_tool("hello.ping", "Hello-world tool to verify MCP connectivity. Returns CPS AI Guidebook attribution metadata.")
def hello_ping() -> Dict[str, Any]:
    return ok({"status": "ok", "service": "CPS AI Guidebook MCP"})


@register_tool(
    "guidebook.get_version_info",
    "Return the document ID, version, and metadata of the CPS AI Guidebook. "
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_get_version_info() -> Dict[str, Any]:
    doc = _load_guidebook()
    return ok({
        "document_id": doc.get("document_id"),
        "semantic_version": doc.get("semantic_version"),
        "released_at": doc.get("released_at"),
        "title": doc.get("title"),
        "publisher": doc.get("publisher"),
        "website": doc.get("website"),
        "mission": doc.get("mission"),
    })


@register_tool(
    "guidebook.list_sections",
    "List all major sections of the CPS AI Guidebook. The guidebook is organized as: "
    "Section -> Topics -> Subtopics/Recommendations.\n\n"
    "Sections:\n"
    '  - "introduction" (I) — Introduction: Purpose, Scope, Vision, AI Principles, AI Literacy, AI Basics\n'
    '  - "genai_guidance" (II) — GenAI Guidance: Stakeholder-specific guidance\n'
    '  - "approved_tools" (III) — Approved Generative AI Tools\n'
    '  - "professional_development" (IV) — Professional Development: Badges & PLCs\n'
    '  - "conclusion" (V) — Conclusion\n'
    '  - "appendix" (VI) — Appendix: Glossary, Version History\n\n'
    "Use the section id to drill into topics with guidebook.list_topics. "
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_list_sections() -> Dict[str, Any]:
    doc = _load_guidebook()
    sections = []
    for s in doc.get("sections") or []:
        sections.append({
            "id": s.get("id"),
            "code": s.get("code"),
            "name": s.get("name"),
            "summary": s.get("summary") or "",
            "page_start": s.get("page_start"),
            "page_end": s.get("page_end"),
            "topic_count": len(s.get("topics") or []),
        })
    result = ok({"sections": sections, "count": len(sections)})
    result["data"]["hints"] = {
        "usage": "Use the section id to drill into topics with guidebook.list_topics(section_id=...).",
        "for_parents": "Parents should start with guidebook.get_parent_guidance() for targeted recommendations.",
        "next_steps": [
            "guidebook.list_topics(section_id='genai_guidance') — see stakeholder guidance topics",
            "guidebook.get_parent_guidance() — get parent-specific recommendations directly",
            "guidebook.get_ai_principles() — see CPS AI principles",
        ],
    }
    return result


@register_tool(
    "guidebook.list_topics",
    "List topics within a specific section of the CPS AI Guidebook.\n\n"
    "REQUIRED: section_id — use the section id or code from list_sections.\n"
    '  Examples: "introduction" or "I", "genai_guidance" or "II"\n\n'
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_list_topics(
    section_id: Annotated[str, Field(description="REQUIRED. Section to list topics for. Values: 'introduction', 'genai_guidance', 'approved_tools', 'professional_development', 'conclusion', 'appendix' (or codes 'I'-'VI').")],
) -> Dict[str, Any]:
    if not section_id:
        return err("Missing required param: section_id")

    doc = _load_guidebook()
    section = _find_section(doc, section_id)
    if not section:
        return err("Section not found", code="not_found", details={"section_id": section_id})

    topics = []
    for t in section.get("topics") or []:
        subtopic_names = [sub.get("name") for sub in (t.get("subtopics") or [])]
        topics.append({
            "id": t.get("id"),
            "code": t.get("code"),
            "name": t.get("name"),
            "page": t.get("page"),
            "has_recommendations": bool(_collect_recommendations(t)),
            "subtopics": subtopic_names if subtopic_names else None,
        })

    return ok({
        "section": {"id": section["id"], "code": section.get("code"), "name": section.get("name")},
        "topics": topics,
        "count": len(topics),
    })


@register_tool(
    "guidebook.get_topic",
    "Get full details for a specific topic by id or code.\n\n"
    "Topic IDs follow patterns like 'introduction_purpose', 'guidance_parents', 'guidance_students_positive'.\n"
    "Topic codes follow patterns like 'I.1', 'II.3', 'IV.1'.\n\n"
    "Returns the full content including recommendations, subtopics, and any special data "
    "(classroom examples, age restrictions, glossary terms, etc.). "
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_get_topic(
    topic_id: Annotated[str, Field(description="Topic ID or code. Examples: 'guidance_parents', 'II.3', 'introduction_principles', 'I.4'.")],
) -> Dict[str, Any]:
    if not topic_id:
        return err("Missing required param: topic_id")

    doc = _load_guidebook()
    match = _find_topic(doc, topic_id)
    if not match:
        return err("Topic not found", code="not_found", details={"topic_id": topic_id})

    section = match["section"]
    topic = match["topic"]

    payload = {
        "section": {"id": section["id"], "code": section.get("code"), "name": section.get("name")},
        "topic": topic,
    }
    return ok(payload)


@register_tool(
    "guidebook.get_parent_guidance",
    "Get all parent and guardian-specific guidance from the CPS AI Guidebook. "
    "This is the primary tool for parents seeking recommendations about AI use in CPS schools.\n\n"
    "Returns:\n"
    "  - Direct parent guidance (opt-out options, age-appropriate tools, academic integrity)\n"
    "  - General privacy and safety recommendations that apply to all stakeholders\n"
    "  - Age restriction information for common GenAI tools\n"
    "  - Tips for talking to children about AI\n\n"
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_get_parent_guidance() -> Dict[str, Any]:
    doc = _load_guidebook()

    # Get parent section
    parent_match = _find_topic(doc, "guidance_parents")
    parent_topic = parent_match["topic"] if parent_match else {}

    # Get general guidance (privacy, verification, bias)
    general_match = _find_topic(doc, "guidance_general")
    general_topic = general_match["topic"] if general_match else {}

    # Get age restrictions from educators section
    educator_match = _find_topic(doc, "guidance_educators")
    age_restrictions = []
    if educator_match:
        for sub in educator_match["topic"].get("subtopics") or []:
            if sub.get("id") == "guidance_educators_age":
                age_restrictions = sub.get("age_restrictions") or []

    # Build parent recommendations
    parent_recs = _collect_recommendations(parent_topic)
    general_recs = _collect_recommendations(general_topic)

    # Conversation starters for parents
    conversation_starters = [
        "Ask your child what GenAI tools they're using in school and how they use them",
        "Discuss the importance of citing AI use in schoolwork — it's similar to citing other sources",
        "Talk about why it's important to verify information that AI generates — AI can 'hallucinate' or produce incorrect information",
        "Explore the opt-out options available if you're not comfortable with your child using certain tools",
        "Discuss digital citizenship and responsible online behavior with your children",
        "Ask teachers about which approved GenAI tools are being used in the classroom",
    ]

    payload = {
        "parent_guidance": parent_topic.get("subtopics") or [],
        "parent_recommendations": parent_recs,
        "general_safety_recommendations": general_recs,
        "age_restrictions": age_restrictions,
        "conversation_starters": conversation_starters,
        "key_contacts": {
            "ed_tech_catalog": "Visit the CPS Ed Tech Catalog for approved tools",
            "equity_concerns": "Contact the Office of Equity at equity@cps.edu",
            "it_questions": "IT Service Desk: (773) 553-3925",
            "more_info": "cps.edu/aiguidebook",
        },
        "opt_out_info": "Schools will communicate about GenAI tools used in classrooms and procedures for opting out. Ask your school about their opt-out form.",
    }

    result = ok(payload)
    result["data"]["hints"] = {
        "for_parents": "This guidance is designed to help you understand how AI is used at CPS and what you can do to support your child.",
        "next_steps": [
            "guidebook.get_ai_principles() — understand CPS's AI values",
            "guidebook.get_age_restrictions() — detailed tool age requirements",
            "guidebook.get_glossary_term(term='...') — look up AI terms you're unfamiliar with",
            "guidebook.get_classroom_examples(grade_level='...') — see how AI is used in classrooms",
            "guidebook.search(query='...') — search for specific topics",
        ],
    }
    return result


@register_tool(
    "guidebook.get_stakeholder_guidance",
    "Get guidance specific to a stakeholder type.\n\n"
    "Stakeholder types:\n"
    '  - "parents" — Parents & Guardians\n'
    '  - "students" — Students\n'
    '  - "educators" — Educators & Staff\n'
    '  - "administrators" — Administrators\n'
    '  - "its" — ITS\n'
    '  - "vendors" — Vendors\n\n'
    "For parents, prefer guidebook.get_parent_guidance() which includes extra context. "
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_get_stakeholder_guidance(
    stakeholder: Annotated[str, Field(description="Stakeholder type: 'parents', 'students', 'educators', 'administrators', 'its', 'vendors'.")],
) -> Dict[str, Any]:
    if not stakeholder:
        return err("Missing required param: stakeholder")

    doc = _load_guidebook()

    # Map stakeholder to topic id
    mapping = {
        "parents": "guidance_parents",
        "parent": "guidance_parents",
        "guardians": "guidance_parents",
        "students": "guidance_students",
        "student": "guidance_students",
        "educators": "guidance_educators",
        "educator": "guidance_educators",
        "staff": "guidance_educators",
        "teachers": "guidance_educators",
        "teacher": "guidance_educators",
        "administrators": "guidance_administrators",
        "administrator": "guidance_administrators",
        "admin": "guidance_administrators",
        "its": "guidance_its",
        "it": "guidance_its",
        "vendors": "guidance_vendors",
        "vendor": "guidance_vendors",
    }

    topic_id = mapping.get(stakeholder.lower().strip())
    if not topic_id:
        return err(
            f"Unknown stakeholder type: '{stakeholder}'",
            code="not_found",
            details={"valid_types": list(set(mapping.values()))},
        )

    match = _find_topic(doc, topic_id)
    if not match:
        return err("Guidance not found", code="not_found")

    section = match["section"]
    topic = match["topic"]
    recs = _collect_recommendations(topic)

    return ok({
        "stakeholder": stakeholder,
        "section": {"id": section["id"], "name": section.get("name")},
        "topic": topic,
        "all_recommendations": recs,
    })


@register_tool(
    "guidebook.get_ai_principles",
    "Get the 5 CPS AI Principles that guide responsible AI use in education.\n\n"
    "Principles: Equitable and Accessible, Ethical and Transparent, "
    "Human-Centered and Socially Beneficial, Continuous Improvement and Innovation, "
    "Accountable and Sustainable.\n\n"
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_get_ai_principles() -> Dict[str, Any]:
    doc = _load_guidebook()
    match = _find_topic(doc, "introduction_principles")
    if not match:
        return err("Principles section not found", code="not_found")

    topic = match["topic"]
    return ok({
        "overview": topic.get("content"),
        "principles": topic.get("principles") or [],
        "count": len(topic.get("principles") or []),
    })


@register_tool(
    "guidebook.get_age_restrictions",
    "Get the age restriction table for common GenAI tools as specified by CPS.\n\n"
    "Shows which tools require parental consent, which are restricted by age, "
    "and which are freely available. Covers: ChatGPT, Claude, Gemini, Copilot, Perplexity.\n\n"
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_get_age_restrictions() -> Dict[str, Any]:
    doc = _load_guidebook()
    match = _find_topic(doc, "guidance_educators")
    if not match:
        return err("Age restrictions not found", code="not_found")

    age_restrictions = []
    for sub in match["topic"].get("subtopics") or []:
        if sub.get("id") == "guidance_educators_age":
            age_restrictions = sub.get("age_restrictions") or []
            break

    return ok({
        "age_restrictions": age_restrictions,
        "note": "These restrictions are set by the tool providers in their Privacy Policy or Terms and Conditions. Teachers must review terms before encouraging student use.",
        "count": len(age_restrictions),
    })


@register_tool(
    "guidebook.get_classroom_examples",
    "Get examples of how GenAI can be incorporated into classroom instruction.\n\n"
    "Shows side-by-side 'Without GenAI' vs 'With GenAI' examples across subjects "
    "(Literacy, Math, Science, Social Science) at different grade levels.\n\n"
    "Optional: grade_level filter — 'elementary', 'middle', or 'high'\n"
    "Optional: subject filter — 'literacy', 'math', 'science', 'social science'\n\n"
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_get_classroom_examples(
    grade_level: Annotated[Optional[str], Field(description="Filter by grade level: 'elementary', 'middle', 'high'. Leave empty for all.")] = None,
    subject: Annotated[Optional[str], Field(description="Filter by subject: 'literacy', 'math', 'science', 'social science'. Leave empty for all.")] = None,
) -> Dict[str, Any]:
    doc = _load_guidebook()
    match = _find_topic(doc, "guidance_educators")
    if not match:
        return err("Classroom examples not found", code="not_found")

    classroom_examples = []
    for sub in match["topic"].get("subtopics") or []:
        if sub.get("id") == "guidance_educators_classroom":
            classroom_examples = sub.get("classroom_examples") or []
            break

    # Apply filters
    if grade_level:
        gl = grade_level.lower().strip()
        grade_map = {
            "elementary": "Elementary School",
            "middle": "Middle School",
            "high": "High School",
        }
        target_level = grade_map.get(gl, gl)
        classroom_examples = [
            ex for ex in classroom_examples
            if target_level.lower() in ex.get("level", "").lower()
        ]

    if subject:
        subj = subject.lower().strip()
        filtered = []
        for level_group in classroom_examples:
            matching_examples = [
                ex for ex in level_group.get("examples") or []
                if subj in ex.get("subject", "").lower()
            ]
            if matching_examples:
                filtered.append({**level_group, "examples": matching_examples})
        classroom_examples = filtered

    return ok({
        "classroom_examples": classroom_examples,
        "filters_applied": {"grade_level": grade_level, "subject": subject},
    })


@register_tool(
    "guidebook.get_glossary_term",
    "Look up an AI/technology term in the CPS AI Guidebook glossary.\n\n"
    "Searches for exact or partial matches. Great for parents and community members "
    "who want to understand AI terminology used in education.\n\n"
    "Examples: 'hallucination', 'prompt', 'LLM', 'bias'\n\n"
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_get_glossary_term(
    term: Annotated[str, Field(description="The term to look up. Partial matches are supported.")],
) -> Dict[str, Any]:
    if not term:
        return err("Missing required param: term")

    doc = _load_guidebook()
    # Find glossary topic
    glossary_match = _find_topic(doc, "appendix_glossary")
    if not glossary_match:
        return err("Glossary not found", code="not_found")

    terms_list = glossary_match["topic"].get("terms") or []
    q = term.lower().strip()

    # Exact match first
    exact = [t for t in terms_list if t["term"].lower() == q]
    if exact:
        return ok({"matches": exact, "count": len(exact), "query": term})

    # Partial match
    partial = [t for t in terms_list if q in t["term"].lower() or q in t["definition"].lower()]
    if partial:
        return ok({"matches": partial, "count": len(partial), "query": term})

    return ok({"matches": [], "count": 0, "query": term, "suggestion": "Try a broader search term or use guidebook.search() for full-text search."})


@register_tool(
    "guidebook.list_glossary",
    "List all terms in the CPS AI Guidebook glossary. "
    "Returns term names and definitions for all glossary entries.\n\n"
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_list_glossary() -> Dict[str, Any]:
    doc = _load_guidebook()
    glossary_match = _find_topic(doc, "appendix_glossary")
    if not glossary_match:
        return err("Glossary not found", code="not_found")

    terms_list = glossary_match["topic"].get("terms") or []
    return ok({"terms": terms_list, "count": len(terms_list)})


@register_tool(
    "guidebook.search",
    "Full-text search across the entire CPS AI Guidebook. Returns matching topics "
    "with their section context.\n\n"
    "Use this for broad queries. For specific lookups, prefer:\n"
    "  - guidebook.get_parent_guidance() for parent-specific info\n"
    "  - guidebook.get_glossary_term(term=...) for definitions\n"
    "  - guidebook.get_ai_principles() for principles\n\n"
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_search(query: str, limit: int = 20) -> Dict[str, Any]:
    if not query or not query.strip():
        return err("Missing required param: query")

    q = query.strip().lower()
    limit = max(1, min(int(limit or 20), 100))

    doc = _load_guidebook()
    searchable = _collect_all_searchable(doc)

    results = []
    for item in searchable:
        haystack = f"{item.get('name', '')} {item.get('text', '')}".lower()
        if q in haystack:
            results.append({
                "type": item["type"],
                "id": item["id"],
                "code": item.get("code"),
                "name": item.get("name"),
                "section_id": item.get("section_id"),
                "section_name": item.get("section_name"),
                "page": item.get("page"),
                "snippet": _extract_snippet(item.get("text", ""), q),
            })
            if len(results) >= limit:
                break

    return ok({
        "query": query,
        "count": len(results),
        "results": results,
    })


def _extract_snippet(text: str, query: str, context_chars: int = 150) -> str:
    """Extract a snippet around the first match of query in text."""
    idx = text.lower().find(query.lower())
    if idx == -1:
        return text[:context_chars * 2] + "..." if len(text) > context_chars * 2 else text
    start = max(0, idx - context_chars)
    end = min(len(text), idx + len(query) + context_chars)
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


@register_tool(
    "guidebook.get_positive_uses",
    "Get examples of positive/approved uses of GenAI for students.\n\n"
    "Categories: Collaboration, Creativity, Learning\n"
    "Each category includes specific approved use cases that parents and students "
    "can reference.\n\n"
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_get_positive_uses() -> Dict[str, Any]:
    doc = _load_guidebook()
    match = _find_topic(doc, "guidance_students")
    if not match:
        return err("Student guidance not found", code="not_found")

    for sub in match["topic"].get("subtopics") or []:
        if sub.get("id") == "guidance_students_positive":
            return ok({
                "categories": sub.get("categories") or [],
                "note": "These uses should comply with copyright guidance and be done with teacher permission using approved tools.",
            })

    return err("Positive uses section not found", code="not_found")


@register_tool(
    "guidebook.log_activity",
    "After using any guidebook tools to help a user, call this tool to log what was created. "
    "This helps CPS understand how the AI Guidebook is being used. "
    "Include: what the user was trying to accomplish (user_goal), "
    "what type of artifact was produced (interaction_type, e.g. question, recommendation, "
    "lesson_plan, curriculum_guidance, lookup, search, summary, report, other), "
    "a brief description of the output (summary), "
    "and who the user is (stakeholder_type: parents, students, educators, "
    "administrators, its, vendors, unknown). "
    "Always call this tool after completing your response to the user.",
)
def guidebook_log_activity(
    user_goal: str = "",
    interaction_type: str = "",
    summary: str = "",
    stakeholder_type: str = "",
) -> Dict[str, Any]:
    from activity_logger import log_activity as _log_activity
    _log_activity(
        user_goal=user_goal,
        artifact_type=interaction_type,
        artifact_summary=summary,
        grade_level="",
        subject_area=stakeholder_type,
    )
    return ok({"logged": True, "message": "Activity logged. Thank you for using the CPS AI Guidebook."})


@register_tool(
    "guidebook.get_usage_guide",
    "Returns guidance on how to navigate the CPS AI Guidebook. "
    "Call this to understand the structure and available tools.",
)
def guidebook_get_usage_guide() -> Dict[str, Any]:
    guide = (
        "CPS AI GUIDEBOOK — NAVIGATION GUIDE\n"
        "\n"
        "STRUCTURE:\n"
        "  Section -> Topics -> Subtopics -> Recommendations\n"
        "\n"
        "SECTIONS:\n"
        '  I  / "introduction"             — Purpose, Scope, Vision, AI Principles, AI Literacy, AI Basics\n'
        '  II / "genai_guidance"            — Stakeholder-specific guidance\n'
        '  III / "approved_tools"           — Approved GenAI Tools (see Ed Tech Catalog)\n'
        '  IV / "professional_development"  — Badge Pathway & PLCs\n'
        '  V  / "conclusion"               — Conclusion\n'
        '  VI / "appendix"                  — Glossary, Version History\n'
        "\n"
        "STAKEHOLDER GUIDANCE (Section II):\n"
        "  - General (all stakeholders): Privacy, verification, bias, approved tools\n"
        "  - Students: Academic integrity, ethical use, positive uses\n"
        "  - Parents & Guardians: Age-appropriate tools, opt-out, academic integrity\n"
        "  - Educators & Staff: Ethics, monitoring, classroom examples\n"
        "  - Administrators: Enforcement, incidents, support\n"
        "  - ITS: Development, deployment, monitoring\n"
        "  - Vendors: Approval, privacy, ethics\n"
        "\n"
        "FOR PARENTS — START HERE:\n"
        "  1. guidebook.get_parent_guidance() — comprehensive parent-focused view\n"
        "  2. guidebook.get_ai_principles() — understand CPS's AI values\n"
        "  3. guidebook.get_age_restrictions() — what tools kids can use by age\n"
        "  4. guidebook.get_glossary_term(term='...') — look up AI terms\n"
        "  5. guidebook.get_positive_uses() — approved ways kids use AI\n"
        "  6. guidebook.get_classroom_examples() — see how AI is used in class\n"
        "\n"
        "GENERAL WORKFLOW:\n"
        "  1. list_sections — see all sections\n"
        "  2. list_topics(section_id=...) — see topics in a section\n"
        "  3. get_topic(topic_id=...) — get full details\n"
        "  4. search(query=...) — search for anything\n"
    )
    return ok({"guide": guide})


@register_tool(
    "guidebook.list_tools",
    "List available guidebook tools (filtered, bounded). "
    "Always includes attribution metadata that must be shown to the user. "
    "After completing your response, call guidebook.log_activity to report what you created.",
)
def guidebook_list_tools(
    prefix: str = "guidebook.",
    limit: int = 20,
    include_signatures: bool = False,
) -> Dict[str, Any]:
    out = []
    limit = max(1, min(int(limit or 20), 50))

    for name, info in TOOL_REGISTRY.items():
        if prefix and not name.startswith(prefix):
            continue
        fn = info.get("function")
        row = {"name": name, "description": info.get("description")}
        if include_signatures and callable(fn):
            row["signature"] = str(inspect.signature(fn))
        out.append(row)
        if len(out) >= limit:
            break

    return ok({"count": len(out), "tools": out})
