# tests/test_life_threatening_interruption.py
#
# Focused regression tests for the live emergency-interruption backport.
#
# Scope under test (app/routes/chat.py only):
#   * six new normalized bleeding variants in EMERGENCY_TRIGGERS
#   * new narrow classifier looks_like_life_threatening_emergency()
#   * suppression of the same-response _next_emergency_prompt() append at the
#     three verified sites (dangerous self-treatment guard, urgent trauma guard,
#     emergency_booking_mode) for life-threatening current messages
#   * preservation of non-life-threatening emergency behavior, urgent-but-not-911
#     behavior, and next-turn affirmative continuation
#
# Test strategy (stdlib unittest, runnable offline):
#   External packages (fastapi, sqlalchemy, openai, twilio, resend) and the app's
#   DB-bound modules (app.config/database/models/schemas) are replaced with light
#   stubs in sys.modules BEFORE importing app.routes.chat, so the REAL routing
#   code in chat() executes end-to-end against a fake in-memory DB session.
#   app.services.mia_service_library is imported for real (pure stdlib).
#   mark_completed_and_notify_office and notify_office_of_lock are patched with
#   spies so notification behavior can be asserted without side effects.
#
#   Classes named *StructuralSupplement* contain clearly labeled supplemental
#   source-structure assertions only. They are NOT integration tests and are not
#   presented as such.

import sys
import re
import types
import uuid
import unittest
from unittest import mock
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# 1) Stub external third-party packages before importing app.routes.chat
# ---------------------------------------------------------------------------

def _module(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeHTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _FakeAPIRouter:
    def post(self, *a, **k):
        return lambda f: f

    def get(self, *a, **k):
        return lambda f: f


if "fastapi" not in sys.modules:
    _module(
        "fastapi",
        APIRouter=_FakeAPIRouter,
        HTTPException=_FakeHTTPException,
        Request=object,
        Depends=lambda *a, **k: None,
    )

if "sqlalchemy" not in sys.modules:
    _module("sqlalchemy", text=lambda s: s, or_=lambda *a, **k: None)
    _module("sqlalchemy.orm", Session=object)

if "openai" not in sys.modules:
    class _FakeOpenAI:
        def __init__(self, *a, **k):
            self.chat = mock.MagicMock()
            self.responses = mock.MagicMock()
    _module("openai", OpenAI=_FakeOpenAI)

if "twilio" not in sys.modules:
    _module("twilio")
    _module("twilio.rest", Client=mock.MagicMock)

if "resend" not in sys.modules:
    _module("resend", api_key=None, Emails=mock.MagicMock())

# ---------------------------------------------------------------------------
# 2) Stub app.config / app.database / app.models / app.schemas
#    (real ones require SQLAlchemy/Pydantic + a database)
# ---------------------------------------------------------------------------

_module("app.config", OPENAI_API_KEY="test-key-not-real")
_module("app.database", SessionLocal=lambda: None, Base=object, engine=None)


class _Expr:
    """Inert stand-in for a SQLAlchemy filter expression."""
    def __and__(self, other): return self
    def __or__(self, other): return self
    def __invert__(self): return self
    def __bool__(self): return True


class _Col:
    """Inert stand-in for a SQLAlchemy column used in class-level expressions."""
    def __eq__(self, other): return _Expr()
    def __ne__(self, other): return _Expr()
    def __hash__(self): return id(self)
    def desc(self): return self
    def asc(self): return self
    def ilike(self, *a, **k): return _Expr()
    def in_(self, *a, **k): return _Expr()
    def is_(self, *a, **k): return _Expr()


class _ModelMeta(type):
    # Class-level attribute access (e.g. Message.created_at) yields a fake column.
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Col()


class _FakeModel(metaclass=_ModelMeta):
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class FakeClient(_FakeModel):
    def __init__(self, **kw):
        self.id = 1
        self.api_key = "test-key"
        self.active = True
        self.office_phone = "(555) 010-0000"
        self.settings = None
        super().__init__(**kw)
    # Unset attributes intentionally raise AttributeError so that
    # getattr(client, "...", default) uses the live defaults
    # (accepts_emergencies=True, accepts_walkins=False, etc.).


class FakeConversation(_FakeModel):
    def __init__(self, **kw):
        self.id = uuid.uuid4()
        self.client_id = 1
        self.visitor_id = "visitor-1"
        self.is_lead = False
        self.lead_status = "new"
        self.lead_name = None
        self.lead_phone = None
        self.lead_email = None
        self.lead_reason = None
        self.lead_reason_source_text = None
        self.lead_name_source_text = None
        self.lead_time_window = None
        self.abuse_strikes = 0
        self.abuse_locked_until = None
        super().__init__(**kw)


class FakeMessage(_FakeModel):
    def __init__(self, **kw):
        self.conversation_id = None
        self.role = None
        self.content = None
        self.created_at = datetime.now(timezone.utc)
        super().__init__(**kw)


class FakeClientFAQ(_FakeModel):
    pass


class FakeFAQEvent(_FakeModel):
    pass


_module(
    "app.models",
    Client=FakeClient,
    Conversation=FakeConversation,
    Message=FakeMessage,
    ClientFAQ=FakeClientFAQ,
    FAQEvent=FakeFAQEvent,
)


class _FakeChatRequest:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeChatResponse:
    def __init__(self, **kw):
        self.reply = kw.get("reply")
        self.conversation_id = kw.get("conversation_id")
        self.meta = kw.get("meta") or {}
        self.__dict__.update(kw)


_module("app.schemas", ChatRequest=_FakeChatRequest, ChatResponse=_FakeChatResponse)

# ---------------------------------------------------------------------------
# 3) Import the REAL module under test
# ---------------------------------------------------------------------------

import importlib

chat_mod = importlib.import_module("app.routes.chat")

# ---------------------------------------------------------------------------
# 4) Fake DB session driving the real chat() routing
# ---------------------------------------------------------------------------


class FakeQuery:
    def __init__(self, db, model):
        self.db = db
        self.model = model

    def filter(self, *a, **k): return self
    def filter_by(self, **k): return self
    def order_by(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def offset(self, *a, **k): return self
    def distinct(self, *a, **k): return self

    def first(self):
        if self.model is FakeClient:
            return self.db.client
        if self.model is FakeConversation:
            return self.db.conversation
        if self.model is FakeMessage:
            # All Message .first() lookups reached in these scenarios are
            # "most recent assistant message" queries.
            for msg in reversed(self.db.messages):
                if msg.role == "assistant":
                    return msg
            return None
        return None

    def all(self):
        if self.model is FakeMessage:
            return list(self.db.messages)
        return []

    def count(self):
        return len(self.all())


class FakeDB:
    def __init__(self, client, conversation, messages=None):
        self.client = client
        self.conversation = conversation
        self.messages = list(messages or [])

    def query(self, model):
        return FakeQuery(self, model)

    def add(self, obj):
        if isinstance(obj, FakeMessage):
            self.messages.append(obj)

    def commit(self): pass
    def refresh(self, obj): pass
    def rollback(self): pass
    def close(self): pass

    def execute(self, *a, **k):
        return SimpleNamespace(scalar=lambda: datetime.now(timezone.utc))


def assistant_msg(conversation, content):
    return FakeMessage(conversation_id=conversation.id, role="assistant", content=content)


def run_chat(user_text, conversation=None, messages=None, client=None):
    """Invoke the real chat() endpoint function against the fake session.

    Returns (response, db, notify_spy, lock_spy).
    """
    client = client or FakeClient()
    conversation = conversation if conversation is not None else FakeConversation()
    db = FakeDB(client, conversation, messages)
    req = SimpleNamespace(
        message=user_text,
        client_key="test-key",
        conversation_id=str(conversation.id),
        visitor_id="visitor-1",
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    with mock.patch.object(chat_mod, "mark_completed_and_notify_office") as notify_spy, \
         mock.patch.object(chat_mod, "notify_office_of_lock") as lock_spy:
        notify_spy.return_value = (False, False, None, None)
        resp = chat_mod.chat(req, request, db)
    return resp, db, notify_spy, lock_spy


# Fragments that must never appear in a standalone life-threatening safety reply.
INTAKE_FRAGMENTS = [
    "first name",
    "phone number",
    "email",
    "day/time",
    "works best",
    "new or returning",
    "Briefly, what",
    "what\u2019s going on",
]

LIVE_911_FRAGMENT = "call 911 or go to the ER now"

BREATHING = [
    "can t breathe", "cant breathe", "cannot breathe",
    "trouble breathing", "difficulty breathing",
]
SWALLOWING = [
    "can t swallow", "cant swallow", "cannot swallow",
    "trouble swallowing", "difficulty swallowing",
]
BLEEDING = [
    "uncontrolled bleeding", "won t stop bleeding", "wont stop bleeding",
    "can t stop bleeding", "cant stop bleeding", "cannot stop bleeding",
    "bleeding won t stop", "bleeding wont stop", "bleeding will not stop",
    "blood everywhere", "bleeding everywhere",
]
SWELLING = ["rapidly worsening swelling", "worsening swelling"]

NEW_BLEEDING_VARIANTS = [
    "can t stop bleeding", "cant stop bleeding", "cannot stop bleeding",
    "bleeding won t stop", "bleeding wont stop", "bleeding will not stop",
]


def assert_standalone_safety_reply(testcase, resp):
    testcase.assertIn(LIVE_911_FRAGMENT, resp.reply)
    testcase.assertNotIn("?", resp.reply)
    for frag in INTAKE_FRAGMENTS:
        testcase.assertNotIn(frag, resp.reply)


# ===========================================================================
# 1) Pure classifier tests — every approved phrase
# ===========================================================================


class TestLifeThreateningClassifierPositive(unittest.TestCase):
    def test_every_approved_phrase_is_life_threatening(self):
        for phrase in BREATHING + SWALLOWING + BLEEDING + SWELLING:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    chat_mod.looks_like_life_threatening_emergency(phrase),
                    f"expected life-threatening: {phrase!r}",
                )

    def test_natural_punctuated_forms_normalize_and_match(self):
        for text in [
            "I can't breathe",
            "I can't swallow properly",
            "My mouth can't stop bleeding!",
            "The bleeding won't stop.",
            "It's rapidly worsening swelling",
        ]:
            with self.subTest(text=text):
                self.assertTrue(chat_mod.looks_like_life_threatening_emergency(text))

    def test_swelling_plus_breathing_combination_heuristic(self):
        self.assertTrue(
            chat_mod.looks_like_life_threatening_emergency(
                "My face is swelling and I\u2019m having trouble breathing."
            )
        )
        self.assertTrue(
            chat_mod.looks_like_life_threatening_emergency(
                "my cheek is swollen and it is hard to breathe"
            )
        )
        self.assertTrue(
            chat_mod.looks_like_life_threatening_emergency(
                "face swollen and hard to swallow"
            )
        )

    def test_uses_norm_text_behavior(self):
        # Mixed case + punctuation must normalize identically to _norm_text.
        self.assertTrue(
            chat_mod.looks_like_life_threatening_emergency("TROUBLE   Breathing!!!")
        )
        self.assertFalse(chat_mod.looks_like_life_threatening_emergency(""))
        self.assertFalse(chat_mod.looks_like_life_threatening_emergency("   "))


class TestLifeThreateningClassifierNegative(unittest.TestCase):
    def test_severe_tooth_pain_is_not_life_threatening(self):
        self.assertFalse(
            chat_mod.looks_like_life_threatening_emergency("I have severe tooth pain")
        )

    def test_knocked_out_tooth_is_not_life_threatening(self):
        self.assertFalse(
            chat_mod.looks_like_life_threatening_emergency("My tooth got knocked out")
        )

    def test_plain_dental_trauma_is_not_life_threatening(self):
        self.assertFalse(
            chat_mod.looks_like_life_threatening_emergency(
                "I fell and hit my mouth and broke my tooth"
            )
        )

    def test_moderate_bleeding_language_is_not_life_threatening(self):
        # "bleeding a lot" is emergency-tier in the live triggers but is NOT in
        # the approved life-threatening list.
        self.assertFalse(
            chat_mod.looks_like_life_threatening_emergency("my gum is bleeding a lot")
        )

    def test_plain_swelling_without_airway_language_is_not_life_threatening(self):
        self.assertFalse(
            chat_mod.looks_like_life_threatening_emergency("my cheek is swollen")
        )


class TestSevereAndKnockedOutRemainEmergencyTier(unittest.TestCase):
    def test_severe_tooth_pain_still_emergency(self):
        # NOTE: live EMERGENCY_TRIGGERS match the substring "severe pain";
        # the literal phrase "severe tooth pain" has never been a live trigger.
        self.assertTrue(chat_mod.looks_like_emergency("I have severe pain in my tooth"))

    def test_knocked_out_tooth_still_emergency(self):
        self.assertTrue(chat_mod.looks_like_emergency("My tooth got knocked out"))


# ===========================================================================
# 2) The six newly added bleeding variants
# ===========================================================================


class TestNewBleedingVariants(unittest.TestCase):
    def test_variants_present_in_emergency_triggers(self):
        for phrase in NEW_BLEEDING_VARIANTS:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, chat_mod.EMERGENCY_TRIGGERS)

    def test_variants_detected_by_broad_emergency_classifier(self):
        for phrase in NEW_BLEEDING_VARIANTS:
            with self.subTest(phrase=phrase):
                self.assertTrue(chat_mod.looks_like_emergency(phrase))

    def test_variants_detected_by_life_threatening_classifier(self):
        for phrase in NEW_BLEEDING_VARIANTS:
            with self.subTest(phrase=phrase):
                self.assertTrue(chat_mod.looks_like_life_threatening_emergency(phrase))

    def test_natural_apostrophe_forms(self):
        for text in [
            "I can't stop bleeding",
            "it just won't stop, the bleeding won't stop",
            "the bleeding will not stop",
        ]:
            with self.subTest(text=text):
                self.assertTrue(chat_mod.looks_like_emergency(text))
                self.assertTrue(chat_mod.looks_like_life_threatening_emergency(text))


# ===========================================================================
# 3) Exact observed phone-stage regression
# ===========================================================================


class TestObservedPhoneStageRegression(unittest.TestCase):
    MESSAGE = "My face is swelling and I\u2019m having trouble breathing."

    def _phone_stage_conversation(self):
        conv = FakeConversation()
        conv.is_lead = True
        conv.lead_reason = "tooth pain"
        conv.lead_name = "Maria"
        msgs = [assistant_msg(conv, "Thanks \u2014 what\u2019s the best phone number to reach you?")]
        return conv, msgs

    def test_safety_reply_stands_alone_with_no_intake_question(self):
        conv, msgs = self._phone_stage_conversation()
        resp, db, notify_spy, _ = run_chat(self.MESSAGE, conversation=conv, messages=msgs)

        # Routed through the main emergency block.
        self.assertEqual(resp.meta.get("mode"), "emergency_booking_mode")
        self.assertTrue(resp.meta.get("emergency_mode"))

        # Existing 911/ER instruction present; NO intake question of any kind.
        assert_standalone_safety_reply(self, resp)
        self.assertNotIn("best phone number", resp.reply)

    def test_previously_captured_fields_unchanged(self):
        conv, msgs = self._phone_stage_conversation()
        run_chat(self.MESSAGE, conversation=conv, messages=msgs)
        self.assertEqual(conv.lead_name, "Maria")
        self.assertEqual(conv.lead_reason, "tooth pain")
        self.assertIsNone(conv.lead_phone)
        self.assertIsNone(conv.lead_email)

    def test_no_completion_or_notification_triggered(self):
        conv, msgs = self._phone_stage_conversation()
        _, _, notify_spy, lock_spy = run_chat(self.MESSAGE, conversation=conv, messages=msgs)
        notify_spy.assert_not_called()
        lock_spy.assert_not_called()
        self.assertNotEqual((conv.lead_status or "").lower(), "completed")


# ===========================================================================
# 4) Interruption coverage for every live intake stage
# ===========================================================================


def _stage_fixtures():
    """(stage_name, conversation, last_assistant_text) for each live stage."""
    fixtures = []

    conv = FakeConversation()
    fixtures.append(("service_reason", conv, "What do you need help with today?"))

    conv = FakeConversation()
    conv.is_lead = True
    conv.lead_reason = "tooth pain"
    fixtures.append(("name", conv, "What\u2019s your first name?"))

    conv = FakeConversation()
    conv.is_lead = True
    conv.lead_reason = "tooth pain"
    conv.lead_name = "Maria"
    fixtures.append(("phone", conv, "Thanks \u2014 what\u2019s the best phone number to reach you?"))

    conv = FakeConversation()
    conv.is_lead = True
    conv.lead_reason = "cleaning/checkup"
    conv.lead_name = "Maria"
    conv.lead_phone = "5551234567"
    fixtures.append(("email", conv, "What\u2019s your email? (You can also type 'skip'.)"))

    conv = FakeConversation()
    conv.is_lead = True
    conv.lead_reason = "cleaning/checkup"
    conv.lead_name = "Maria"
    conv.lead_phone = "5551234567"
    conv.lead_email = "maria@example.com"
    fixtures.append(("preferred_day_time", conv, "What day/time works best for you?"))

    conv = FakeConversation()
    conv.is_lead = True
    conv.lead_reason = "cleaning/checkup"
    conv.lead_name = "Maria"
    conv.lead_phone = "5551234567"
    conv.lead_email = "maria@example.com"
    conv.lead_time_window = "Monday morning"
    fixtures.append(("new_or_returning", conv, "One quick question \u2014 Maria, are you a new or returning patient?"))

    return fixtures


class TestInterruptionAtEveryIntakeStage(unittest.TestCase):
    LT_MESSAGE = "My face is swelling and I\u2019m having trouble breathing."

    def test_life_threatening_message_interrupts_every_stage(self):
        for stage, conv, last_q in _stage_fixtures():
            with self.subTest(stage=stage):
                msgs = [assistant_msg(conv, last_q)]
                snapshot = {
                    "lead_name": conv.lead_name,
                    "lead_phone": conv.lead_phone,
                    "lead_email": conv.lead_email,
                    "lead_reason": conv.lead_reason,
                    "lead_time_window": conv.lead_time_window,
                }
                resp, _, notify_spy, _ = run_chat(self.LT_MESSAGE, conversation=conv, messages=msgs)

                self.assertEqual(resp.meta.get("mode"), "emergency_booking_mode")
                assert_standalone_safety_reply(self, resp)
                notify_spy.assert_not_called()
                for field, value in snapshot.items():
                    self.assertEqual(getattr(conv, field), value,
                                     f"{field} changed at stage {stage}")


class TestEachLifeThreateningCategoryInterrupts(unittest.TestCase):
    CATEGORY_MESSAGES = {
        "breathing": "I\u2019m having trouble breathing",
        "swallowing": "I can\u2019t swallow",
        "uncontrolled_bleeding": "I have uncontrolled bleeding from my mouth",
        "worsening_swelling": "I have rapidly worsening swelling in my jaw",
    }

    def test_each_category_interrupts_phone_stage(self):
        for category, message in self.CATEGORY_MESSAGES.items():
            with self.subTest(category=category):
                conv = FakeConversation()
                conv.is_lead = True
                conv.lead_reason = "tooth pain"
                conv.lead_name = "Maria"
                msgs = [assistant_msg(conv, "Thanks \u2014 what\u2019s the best phone number to reach you?")]
                resp, _, notify_spy, _ = run_chat(message, conversation=conv, messages=msgs)
                self.assertEqual(resp.meta.get("mode"), "emergency_booking_mode")
                assert_standalone_safety_reply(self, resp)
                notify_spy.assert_not_called()


# ===========================================================================
# 5/6) Field preservation + no completion/notify with name AND phone captured
# ===========================================================================


class TestNoCompletionWhenNameAndPhoneAlreadyCaptured(unittest.TestCase):
    def test_life_threatening_message_does_not_complete_lead(self):
        conv = FakeConversation()
        conv.is_lead = True
        conv.lead_reason = "tooth pain"
        conv.lead_name = "Maria"
        conv.lead_phone = "5551234567"
        msgs = [assistant_msg(conv, "What day/time works best for you?")]

        resp, _, notify_spy, lock_spy = run_chat(
            "My face is swelling and I can\u2019t breathe", conversation=conv, messages=msgs
        )

        self.assertEqual(resp.meta.get("mode"), "emergency_booking_mode")
        assert_standalone_safety_reply(self, resp)
        notify_spy.assert_not_called()
        lock_spy.assert_not_called()
        self.assertNotEqual((conv.lead_status or "").lower(), "completed")
        self.assertEqual(conv.lead_name, "Maria")
        self.assertEqual(conv.lead_phone, "5551234567")


# ===========================================================================
# 7/8/9) Non-life-threatening behavior preserved
# ===========================================================================


class TestNonLifeThreateningEmergencyBehaviorPreserved(unittest.TestCase):
    def test_severe_tooth_pain_keeps_emergency_intake_prompt(self):
        resp, _, _, _ = run_chat("I have severe pain in my tooth")
        self.assertEqual(resp.meta.get("mode"), "emergency_booking_mode")
        self.assertIn(LIVE_911_FRAGMENT, resp.reply)
        self.assertIn("To help quickly, what\u2019s your first name?", resp.reply)

    def test_knocked_out_tooth_keeps_emergency_intake_prompt(self):
        resp, _, _, _ = run_chat("My tooth got knocked out")
        self.assertEqual(resp.meta.get("mode"), "emergency_booking_mode")
        self.assertIn(LIVE_911_FRAGMENT, resp.reply)
        self.assertIn("To help quickly, what\u2019s your first name?", resp.reply)

    def test_dental_trauma_without_life_threat_keeps_urgent_guard_prompt(self):
        resp, _, _, _ = run_chat("I fell and hit my mouth and broke my tooth")
        self.assertEqual(resp.meta.get("mode"), "urgent_dental_safety_guard")
        self.assertIn(LIVE_911_FRAGMENT, resp.reply)
        self.assertIn("To help quickly, what\u2019s your first name?", resp.reply)

    def test_dental_trauma_with_life_threat_suppresses_prompt_at_urgent_guard(self):
        resp, _, notify_spy, _ = run_chat(
            "I fell and broke my tooth and there is blood everywhere"
        )
        self.assertEqual(resp.meta.get("mode"), "urgent_dental_safety_guard")
        assert_standalone_safety_reply(self, resp)
        notify_spy.assert_not_called()

    def test_dangerous_self_treatment_emergency_keeps_prompt_when_not_life_threatening(self):
        resp, _, _, _ = run_chat(
            "The pain is unbearable, should I pull my tooth out with pliers"
        )
        self.assertEqual(resp.meta.get("mode"), "dangerous_dental_self_treatment_guard")
        self.assertTrue(resp.meta.get("emergency_mode"))
        self.assertIn(LIVE_911_FRAGMENT, resp.reply)
        self.assertIn("To help quickly, what\u2019s your first name?", resp.reply)

    def test_dangerous_self_treatment_with_life_threat_suppresses_prompt(self):
        resp, _, notify_spy, _ = run_chat(
            "Should I pull my tooth out with pliers, there is blood everywhere and it wont stop bleeding"
        )
        self.assertEqual(resp.meta.get("mode"), "dangerous_dental_self_treatment_guard")
        self.assertTrue(resp.meta.get("emergency_mode"))
        self.assertIn(LIVE_911_FRAGMENT, resp.reply)
        self.assertNotIn("?", resp.reply)
        for frag in INTAKE_FRAGMENTS:
            self.assertNotIn(frag, resp.reply)
        notify_spy.assert_not_called()

    def test_urgent_but_not_911_behavior_unchanged(self):
        resp, _, _, _ = run_chat("I need an appointment asap for tooth pain")
        self.assertEqual(resp.meta.get("mode"), "urgent_priority_lead")
        self.assertIn("mark this as urgent", resp.reply)
        self.assertIn("What\u2019s your first name?", resp.reply)


# ===========================================================================
# 10) Next-turn affirmative continuation unchanged
# ===========================================================================


class TestNextTurnAffirmativeContinuationUnchanged(unittest.TestCase):
    def test_affirmative_after_emergency_message_still_continues_intake(self):
        conv = FakeConversation()
        during, _ = chat_mod.get_emergency_defaults()
        msgs = [assistant_msg(conv, during)]

        resp, _, _, _ = run_chat("yes", conversation=conv, messages=msgs)

        self.assertEqual(resp.meta.get("mode"), "emergency_intake_continue")
        self.assertEqual(resp.reply, "To help quickly, what\u2019s your first name?")

    def test_affirmative_continuation_asks_phone_when_name_captured(self):
        conv = FakeConversation()
        conv.is_lead = True
        conv.lead_name = "Maria"
        during, _ = chat_mod.get_emergency_defaults()
        msgs = [assistant_msg(conv, during)]

        resp, _, _, _ = run_chat("yes", conversation=conv, messages=msgs)

        self.assertEqual(resp.meta.get("mode"), "emergency_intake_continue")
        self.assertEqual(
            resp.reply,
            "Thanks \u2014 what\u2019s the best phone number to reach you right now?",
        )


# ===========================================================================
# Supplemental STRUCTURAL assertions (clearly labeled; NOT integration tests)
# ===========================================================================


class TestPatchStructuralSupplement(unittest.TestCase):
    """Supplemental source-structure checks of app/routes/chat.py.

    These assert the shape of the patch itself (gates present at the three
    same-response sites; next-turn sites left ungated). They are structural
    assertions only and are not integration tests.
    """

    @classmethod
    def setUpClass(cls):
        with open(chat_mod.__file__, "r", encoding="utf-8", newline="") as f:
            cls.src = f.read()

    def test_exactly_three_gate_references_at_append_sites(self):
        gates = self.src.count("looks_like_life_threatening_emergency(user_text)")
        self.assertEqual(gates, 3)

    def test_emergency_booking_mode_append_is_gated(self):
        self.assertIn(
            'if not looks_like_life_threatening_emergency(user_text):\r\n'
            '                reply_text += "\\n\\n" + _next_emergency_prompt(conversation)',
            self.src,
        )

    def test_next_turn_continue_site_is_ungated(self):
        # emergency_intake_continue still assigns the prompt directly.
        self.assertIn(
            "reply_text = _next_emergency_prompt(conversation)",
            self.src,
        )

    def test_next_turn_followup_site_is_ungated(self):
        # emergency_followup_intake still assigns the prompt directly
        # ("# Otherwise continue normal emergency intake" branch).
        self.assertIn(
            "# Otherwise continue normal emergency intake\r\n"
            "        next_prompt = _next_emergency_prompt(conversation)",
            self.src,
        )

    def test_next_emergency_prompt_function_unchanged(self):
        # The helper itself must remain the original 4-branch prompt sequence.
        self.assertIn('def _next_emergency_prompt(conversation) -> str:', self.src)
        self.assertIn('return "To help quickly, what\u2019s your first name?"', self.src)
        self.assertIn('return "Thanks \u2014 what\u2019s the best phone number to reach you right now?"', self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
