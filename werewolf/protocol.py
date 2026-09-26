"""Protocol constants and strict, transport-independent validation."""
import json
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

VERSION = "1.1"
RULE_VERSION = "9p-seer-witch-hunter/1.0"
MAX_BYTES = 1048576
ROLES = ["werewolf"] * 3 + ["villager"] * 3 + ["seer", "witch", "hunter"]
RULES = {
    "role_counts": dict(Counter(ROLES)),
    "win_mode": "kill_all_wolves_or_eliminate_villagers_or_specials",
    "win_check_order": ["resolve_hunter", "wolves_eliminated", "villagers_eliminated", "specials_eliminated"],
    "sheriff_enabled": False, "wolf_self_destruct_enabled": False, "wolf_chat_enabled": False,
    "wolf_max_rounds": 3, "wolf_vote_policy": "unique_plurality_else_revote_final_random_top",
    "wolf_all_abstain_policy": "revote_then_no_kill", "wolf_allow_self_target": True,
    "wolf_allow_teammate_target": True, "wolf_allow_no_kill": True,
    "witch_self_save": "every_night", "witch_max_potions_per_night": 1,
    "witch_poison_self_allowed": False, "witch_kill_visibility": "while_antidote_available",
    "seer_repeat_allowed": True, "hunter_poison_blocks_shot": True,
    "day_vote_policy": "one_pk_revote_then_no_exile",
    "last_words_policy": "first_night_and_exile_unless_game_ended",
    "speech_order_policy": "random_first_day_rotate_start_keep_direction",
}
LIMITS = {"action_timeout_ms": 60000, "speech_max_codepoints": 500,
          "max_days": 30, "max_message_bytes": MAX_BYTES}
REQUEST_TYPES = {"werewolves_act", "werewolves_revote", "witch_act", "seer_act",
                 "hunter_act", "speech", "speech_dying", "vote"}


def uid():
    return uuid4().hex


def utc(timestamp=None):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if timestamp is not None else datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ProtocolError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def strict_loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES:
            raise ValueError("invalid size")
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        # Escaped, unpaired UTF-16 surrogates are not valid UTF-8 text.
        json.dumps(value, ensure_ascii=False).encode("utf-8")
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProtocolError("INVALID_JSON") from exc


def exact(obj, fields):
    if not isinstance(obj, dict) or set(obj) != set(fields):
        raise ProtocolError("INVALID_MESSAGE")


def validate_client(message):
    exact(message, ["protocol_version", "type", "message_id", "game_id", "request_id", "content"])
    if message["protocol_version"] != VERSION:
        raise ProtocolError("UNSUPPORTED_VERSION")
    for key in ("type", "message_id"):
        if not isinstance(message[key], str) or not 1 <= len(message[key]) <= 128:
            raise ProtocolError("INVALID_MESSAGE")
    for key in ("game_id", "request_id"):
        if message[key] is not None and (not isinstance(message[key], str) or not 1 <= len(message[key]) <= 128):
            raise ProtocolError("INVALID_MESSAGE")
    c, kind = message["content"], message["type"]
    if kind == "auth":
        exact(c, ["agent_id", "token", "client_version"])
        if any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in c.values()):
            raise ProtocolError("INVALID_MESSAGE")
    elif kind == "ready":
        exact(c, [])
    elif kind == "sync":
        exact(c, ["last_seq"])
        if type(c["last_seq"]) is not int or c["last_seq"] < 0:
            raise ProtocolError("INVALID_MESSAGE")
    elif kind == "action":
        if not isinstance(c, dict):
            raise ProtocolError("INVALID_MESSAGE")
    else:
        raise ProtocolError("INVALID_MESSAGE")
    if kind in {"auth", "ready"} and (message["game_id"] is not None or message["request_id"] is not None):
        raise ProtocolError("INVALID_MESSAGE")
    if kind in {"action", "sync"} and message["game_id"] is None:
        raise ProtocolError("INVALID_MESSAGE")
    if (kind == "action") != (message["request_id"] is not None):
        raise ProtocolError("INVALID_MESSAGE")
    return message


def client_message(kind, content, game_id=None, request_id=None):
    return dict(protocol_version=VERSION, type=kind, message_id=uid(), game_id=game_id,
                request_id=request_id, content=content)


def envelope(kind, content, game_id=None, player_id=None, request_id=None, seq=None):
    return dict(protocol_version=VERSION, type=kind, message_id=uid(), game_id=game_id,
                player_id=player_id, request_id=request_id, seq=seq, sent_at=utc(), content=content)


def validate_action(kind, request, action):
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        raise ProtocolError("INVALID_MESSAGE")
    name = action["action"]
    allowed = {
        "werewolves_act": ["kill_vote"], "werewolves_revote": ["kill_vote"],
        "witch_act": ["save", "poison", "pass"], "seer_act": ["inspect"],
        "hunter_act": ["shoot", "pass"], "speech": ["speak"],
        "speech_dying": ["speak"], "vote": ["vote"],
    }.get(kind, [])
    if name not in allowed:
        raise ProtocolError("ILLEGAL_ACTION")
    exact(action, ["action"] if name == "pass" else ["action", "text" if name == "speak" else "target"])
    if name == "pass":
        return
    if name == "speak":
        text = action["text"]
        if not isinstance(text, str):
            raise ProtocolError("INVALID_MESSAGE")
        if not text.strip():
            raise ProtocolError("EMPTY_TEXT")
        if len(text) > request["max_codepoints"]:
            raise ProtocolError("TEXT_TOO_LONG")
        return
    if name in {"save", "poison"}:
        potion = "antidote" if name == "save" else "poison"
        if not request[f"{potion}_remaining"]:
            raise ProtocolError("NO_ANTIDOTE" if name == "save" else "NO_POISON")
        if name not in request["legal_actions"]:
            raise ProtocolError("ILLEGAL_ACTION")
    target = action["target"]
    if name == "kill_vote" and target is None and request["allow_abstain"]:
        return
    legal = request[f"legal_{name}_targets"] if name in {"save", "poison"} else request["legal_targets"]
    if type(target) is not int or target not in legal:
        raise ProtocolError("ILLEGAL_TARGET")


def default_action(kind):
    if kind.startswith("werewolves_"):
        return {"action": "kill_vote", "target": None}
    if kind in {"witch_act", "hunter_act"}:
        return {"action": "pass"}
    if kind == "vote":
        return {"action": "vote", "target": 0}
    return None


def top_choices(votes, *, wolf=False):
    counts = Counter(v["target"] for v in votes if v["target"] is not None and (wolf or v["target"] != 0))
    return sorted(k for k, count in counts.items() if count == max(counts.values())) if counts else []
