"""Curated interview question bank and project ideas, per skill category (internest_skills.models.Discipline).

Question kinds (chosen per required skill by interview_service.build_questions):
    depth      the skill is verified: confirm the level holds in a realistic task
    subskill   the assessment flagged an area (lag): probe exactly that area
    evidence   listed / related but not verified: look for concrete evidence (STAR)
    gap        required but not shown: baseline and learning plan
Texts use %(skill)s, %(area)s and %(role)s and are translated when a kit is built (gettext_lazy).
"""
from django.utils.translation import gettext_lazy as _

KINDS = ("depth", "subskill", "evidence", "gap")
DIFFICULTIES = ("easy", "medium", "hard")
BASE_DIFFICULTY = {"gap": 0, "evidence": 1, "subskill": 1, "depth": 2, "behavioral": 1}
SENIORITY_SHIFT = {"intern": -1, "junior": 0, "mid": 1}

# Shared per kind: why the question is asked, how to score the answer, what to ask next.
KIND_META = {
    "depth": {
        "purpose": _("Confirm the verified %(skill)s level holds up in a realistic task."),
        "criteria": (_("Explains the reasoning behind each step, not only the result"),
                     _("Names trade-offs or risks and how they were handled"),
                     _("Uses correct terminology for %(skill)s")),
        "follow_ups": (_("What would you do differently with twice the data or half the time?"),
                       _("How did you check that the result was correct?")),
    },
    "subskill": {
        "purpose": _("Probe %(area)s, the area the %(skill)s assessment flagged for improvement."),
        "criteria": (_("Shows what they have practised in %(area)s since the assessment"),
                     _("Gives a correct, concrete example rather than a definition"),
                     _("Knows where to look things up when unsure")),
        "follow_ups": (_("What is still hard for you in %(area)s?"),
                       _("How would you practise it during the internship?")),
    },
    "evidence": {
        "purpose": _("Find real evidence for %(skill)s, which is not verified on Internest yet."),
        "criteria": (_("Describes a specific situation, their own role and the outcome (STAR)"),
                     _("Details stay consistent and specific when probed"),
                     _("Can show or describe an artefact (file, link, repository, report)")),
        "follow_ups": (_("Which part did you do yourself, and which part did others do?"),
                       _("What was the hardest problem and how did you solve it?")),
    },
    "gap": {
        "purpose": _("Gauge the baseline and learning plan for %(skill)s, a required skill not shown yet."),
        "criteria": (_("Is honest about the current level"),
                     _("Has a concrete, realistic plan to learn it"),
                     _("Connects a related skill they already have")),
        "follow_ups": (_("What would your first two weeks of learning look like?"),
                       _("Which related skill of yours will help you learn it faster?")),
    },
}

# discipline -> kind -> (question, expected evidence)
BANK = {
    "computing": {
        "depth": (_("Walk me through how you would build a small %(skill)s solution for a real task in this role, from requirements to testing."),
                  _("A clear plan: inputs and outputs, structure, edge cases, and how it is tested and shipped.")),
        "subskill": (_("Here is a short %(area)s problem from our daily work: how would you approach it in %(skill)s?"),
                     _("A step-by-step approach with correct use of %(area)s and a way to verify the answer.")),
        "evidence": (_("Tell me about a project where you used %(skill)s. What did you build and what broke along the way?"),
                     _("A specific project, their own contribution, a bug or problem they fixed, and a link to the code if available.")),
        "gap": (_("This role uses %(skill)s. What do you already know about it, and how would you get productive in it quickly?"),
                _("A basic idea of what %(skill)s is used for, related tools they know, and a learning plan with resources.")),
    },
    "business": {
        "depth": (_("Take a realistic %(skill)s task for this role, for example a quick analysis for a manager. How would you approach it and present the result?"),
                  _("A structured approach, the data or sources used, stated assumptions and a clear recommendation.")),
        "subskill": (_("Imagine a manager asks you for help with %(area)s tomorrow. What would you deliver and how?"),
                     _("A concrete deliverable, a correct method for %(area)s and a check of the numbers.")),
        "evidence": (_("Describe a time you used %(skill)s to help a team, a client or a student activity make a decision."),
                     _("A real situation, the numbers or facts used, their own role and the decision or result.")),
        "gap": (_("We use %(skill)s in this role. How would you build a working level in your first month?"),
                _("Awareness of what %(skill)s involves, a realistic plan and transferable experience.")),
    },
    "media": {
        "depth": (_("Here is a short brief for this role. How would you deliver it with %(skill)s, from first idea to final file?"),
                  _("A clear creative process: research, concepts, feedback rounds, delivery formats and deadlines.")),
        "subskill": (_("Your %(skill)s work needs stronger %(area)s. Show or describe how you would improve one of your pieces."),
                     _("Specific changes tied to %(area)s and the reason each change makes the work better.")),
        "evidence": (_("Show us a piece from your portfolio that uses %(skill)s. What was the brief and what did you decide?"),
                     _("A portfolio piece, the goal behind it, the choices they made and the feedback it received.")),
        "gap": (_("This role needs %(skill)s. Which tools or related work could you start from, and how would you learn the rest?"),
                _("Related creative experience, familiarity with the tools and a practice plan with small deliverables.")),
    },
    "law": {
        "depth": (_("Give a short %(skill)s answer to a realistic question from this role: which sources would you use and how would you structure it?"),
                  _("Correct sources (laws, cases, contracts), a clear structure and a practical conclusion.")),
        "subskill": (_("Here is a short situation involving %(area)s. What would you check first, and why?"),
                     _("The relevant rules for %(area)s, the order of checks and the risks they would flag.")),
        "evidence": (_("Tell me about a time you used %(skill)s in a course, a moot court or an internship. What was your part?"),
                     _("A specific case or document, their own research or drafting, and what they learned.")),
        "gap": (_("This role needs %(skill)s. How would you prepare for your first assignment in it?"),
                _("Awareness of the basics, the sources they would study and a realistic preparation plan.")),
    },
    "engineering": {
        "depth": (_("Take a realistic %(skill)s problem from this role. How would you solve it, and how would you check the result is safe and correct?"),
                  _("A sound method, the calculations or tools used, the standards followed and a verification step.")),
        "subskill": (_("How would you handle %(area)s in a small project we give you on day one?"),
                     _("A correct approach to %(area)s, the tools or formulas used and how they would check the work.")),
        "evidence": (_("Describe a lab, project or team task where you used %(skill)s. What decision did you make and why?"),
                     _("A concrete project, their technical decisions, the constraints they faced and the outcome.")),
        "gap": (_("This role uses %(skill)s. What foundations do you have, and how would you close the gap?"),
                _("Related coursework, the fundamentals they know and a concrete learning plan.")),
    },
    "health": {
        "depth": (_("Walk me through how you would apply %(skill)s in a realistic case from this role, step by step."),
                  _("A safe, ordered procedure, the reasons behind each step and when to escalate.")),
        "subskill": (_("In a case that involves %(area)s, what would you watch for and what would you do first?"),
                     _("The key signs or risks in %(area)s, correct first actions and who to involve.")),
        "evidence": (_("Tell me about a training, a clinical rotation or a project where you used %(skill)s."),
                     _("A specific situation, their own role, what went well and what they learned.")),
        "gap": (_("This role needs %(skill)s. How would you get ready safely, and under whose supervision?"),
                _("Awareness of the basics, respect for supervision and a clear learning plan.")),
    },
    "general": {
        "depth": (_("Give an example of %(skill)s at its best in your work. What made it effective?"),
                  _("A concrete example, a visible result and the habits behind it.")),
        "subskill": (_("What have you done recently to improve %(area)s? Give an example."),
                     _("A real example of practice in %(area)s and the improvement it brought.")),
        "evidence": (_("Tell me about a time %(skill)s made a difference to a team or a project you were part of."),
                     _("A specific situation, their actions and a result others noticed.")),
        "gap": (_("How would you build %(skill)s during this internship? What support would you ask for?"),
                _("Self-awareness, a realistic plan and openness to feedback.")),
    },
}

BEHAVIORAL = (
    {"question": _("Why this %(role)s role, and what would you like to learn in it?"),
     "purpose": _("Check motivation and fit with the role."),
     "expected_evidence": _("Specific reasons linked to the role and the company, and clear learning goals."),
     "criteria": (_("Knows what the role involves"), _("Gives personal, specific reasons"),
                  _("Learning goals match what the role offers")),
     "follow_ups": (_("What would make this internship a success for you?"), _("What do you know about our work so far?"))},
    {"question": _("Tell me about a time you received critical feedback. What did you change?"),
     "purpose": _("Check how the candidate takes feedback and improves."),
     "expected_evidence": _("A real piece of feedback, the change they made and the result."),
     "criteria": (_("Describes the feedback without blaming others"), _("Took a concrete action"),
                  _("Can show the result of the change")),
     "follow_ups": (_("How do you ask for feedback today?"), _("What feedback are you working on right now?"))},
    {"question": _("Describe a deadline you almost missed. How did you handle it?"),
     "purpose": _("Check ownership and time management."),
     "expected_evidence": _("How they planned, communicated early and still delivered."),
     "criteria": (_("Takes ownership"), _("Told the team early"), _("Explains what they do differently now")),
     "follow_ups": (_("How do you plan a week with several deadlines?"), _("When do you ask for help?"))},
)

# discipline -> project ideas (title, description) that prove a missing skill
PROJECT_IDEAS = {
    "computing": (
        (_("A small %(skill)s tool that solves a real problem"),
         _("Build a small tool with %(skill)s for a problem you or your faculty actually have, and publish the code with a README and screenshots.")),
        (_("Rebuild one feature of an app you use"),
         _("Recreate one feature of an app you use every day with %(skill)s, then write down what you would improve.")),
    ),
    "business": (
        (_("A one-page %(skill)s analysis of a real company"),
         _("Pick a listed Egyptian company or a local shop and produce a one-page analysis using %(skill)s, with sources and a clear recommendation.")),
        (_("Help a student club with %(skill)s"),
         _("Offer a student club a small %(skill)s deliverable (a budget, a campaign plan, a report) and collect written feedback.")),
    ),
    "media": (
        (_("A three-piece %(skill)s case study"),
         _("Create three pieces for a real or imaginary brand using %(skill)s and present the brief, the process and the result as a case study.")),
        (_("Redesign something you see every day"),
         _("Redo a poster, a page or a video you see often with %(skill)s and explain every change you made.")),
    ),
    "law": (
        (_("A short legal memo using %(skill)s"),
         _("Write a two-page memo on a recent legal question using %(skill)s, citing your sources properly.")),
        (_("A plain-language guide"),
         _("Turn a common contract or regulation into a plain-language guide for students that shows your %(skill)s.")),
    ),
    "engineering": (
        (_("A small %(skill)s design with calculations"),
         _("Design a small component or system using %(skill)s, document your assumptions and calculations, and include a simple test or simulation.")),
        (_("Improve a lab experiment"),
         _("Take a lab experiment from your course, improve one part of it using %(skill)s and report the before and after.")),
    ),
    "health": (
        (_("A %(skill)s awareness sheet"),
         _("Prepare an evidence-based one-page sheet that applies %(skill)s for patients or students, reviewed by a supervisor.")),
        (_("A case reflection"),
         _("Write up a training case where %(skill)s mattered: what happened, what was done and what you learned.")),
    ),
    "general": (
        (_("Lead a small %(skill)s task in a team"),
         _("Volunteer to lead one task in a student activity that needs %(skill)s, and keep a short log of decisions and results.")),
        (_("A 30-day %(skill)s challenge"),
         _("Practise %(skill)s every day for 30 days and share a short summary of your progress at the end.")),
    ),
}


def _fmt(text, **values) -> str:
    return str(text) % values if values else str(text)


def bank_for(discipline: str) -> dict:
    return BANK.get(discipline) or BANK["general"]


def difficulty(kind: str, seniority: str, score=None) -> str:
    level = BASE_DIFFICULTY[kind]
    if kind == "depth" and score is not None and score < 85:
        level -= 1  # verified, but not by a wide margin: a medium-depth question first
    if kind != "behavioral":
        level += SENIORITY_SHIFT.get(seniority, 0)
    return DIFFICULTIES[max(0, min(2, level))]


def skill_question(kind: str, *, skill: str, discipline: str, area: str = "", seniority: str = "intern", score=None) -> dict:
    """One question item for `skill` (translated in the active language)."""
    values = {"skill": skill, "area": area or skill}
    question, evidence = bank_for(discipline)[kind]
    meta = KIND_META[kind]
    return {
        "question": _fmt(question, **values), "purpose": _fmt(meta["purpose"], **values),
        "expected_evidence": _fmt(evidence, **values),
        "criteria": [_fmt(c, **values) for c in meta["criteria"]],
        "follow_ups": [_fmt(f, **values) for f in meta["follow_ups"]],
        "difficulty": difficulty(kind, seniority, score), "skill": skill, "kind": kind, "source": "bank",
    }


def behavioral_question(index: int, *, role: str, seniority: str = "intern") -> dict:
    item = BEHAVIORAL[index % len(BEHAVIORAL)]
    values = {"role": role}
    return {
        "question": _fmt(item["question"], **values), "purpose": str(item["purpose"]),
        "expected_evidence": str(item["expected_evidence"]), "criteria": [str(c) for c in item["criteria"]],
        "follow_ups": [str(f) for f in item["follow_ups"]], "difficulty": difficulty("behavioral", seniority),
        "skill": "", "kind": "behavioral", "source": "bank",
    }


def project_idea(skill_name: str, discipline: str, seed: int = 0) -> dict:
    """A curated project that proves `skill_name`; `seed` (e.g. the skill id) picks one deterministically."""
    ideas = PROJECT_IDEAS.get(discipline) or PROJECT_IDEAS["general"]
    title, description = ideas[seed % len(ideas)]
    return {"title": _fmt(title, skill=skill_name), "description": _fmt(description, skill=skill_name)}
