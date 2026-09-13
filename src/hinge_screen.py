"""Parse Hinge Android UI dumps (package, matches, profile, discover)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

PACKAGE = "co.hinge.app"

RESOURCE_PAGE_TITLE = f"{PACKAGE}:id/pageTitle"
RESOURCE_COMPOSER = f"{PACKAGE}:id/messageComposition"
RESOURCE_SEND = f"{PACKAGE}:id/sendChatButton"
RESOURCE_MIC = f"{PACKAGE}:id/microphoneButton"
RESOURCE_PREF_BUTTON = f"{PACKAGE}:id/button"
RESOURCE_BACK = f"{PACKAGE}:id/back"

PROMPT_RE = re.compile(r"^Prompt:\s*(.+?)\s+Answer:\s*(.+)\s*$", re.I | re.DOTALL)
PHOTO_RE = re.compile(r"^(.+?)['’]s photo\s*$", re.I)
MESSAGE_RE = re.compile(r"^\s*(You|[^:]+):\s*(.*?)\s*$", re.DOTALL)
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
NAME_AGE_RE = re.compile(r"^([A-Za-z][\w'’.\- ]{1,40}),\s*(\d{2})\s*$")

SCREEN_OFF = "off_hinge"
SCREEN_MATCHES = "matches_list"
SCREEN_CHAT = "match_chat"
SCREEN_PROFILE = "match_profile"
SCREEN_DISCOVER = "discover"
SCREEN_UNKNOWN = "unknown"

BASIC_LABELS = {
    "age",
    "gender",
    "sexuality",
    "height",
    "location",
    "job",
    "education",
    "school",
    "college or university",
    "languages spoken",
    "ethnicity",
    "religion",
    "politics",
    "drinking",
    "smoking",
    "cannabis",
    "drugs",
    "kids",
    "family plans",
    "pets",
    "relationship type",
    "dating intentions",
    "looking for",
    "pronouns",
    "hometown",
    "home town",
}

LABEL_ALIASES = {
    "college or university": "school",
    "education": "school",
    "home town": "hometown",
    "dating intentions": "looking for",
}

SKIP_NAMES = {
    "matches",
    "discover",
    "standouts",
    "likes you",
    "chat",
    "profile",
    "more",
    "back",
    "hinge",
    "start chat",
    "send a message",
    "your turn",
    "their turn",
    "hidden",
    "hidden matches",
    "new",
    "today",
    "yesterday",
    "liked",
    "active",
    "online",
    "search",
    "settings",
    "record voice note",
    "send message",
    "we met",
}

CHROME_TEXT = SKIP_NAMES | {
    "record voice note",
    "send message",
    "verified",
    "selfie verified",
    "undo last skip",
    "dating preferences",
}

PROMPT_STEMS = (
    "the way to win me over",
    "i'm looking for",
    "i go crazy for",
    "my simple pleasures",
    "a life goal of mine",
    "together we could",
    "i'm overly competitive about",
    "you should not go out with me if",
    "typical sunday",
    "my most irrational fear",
    "i geek out on",
    "the dorkiest thing about me",
    "dating me is like",
    "unusual skill",
    "i recently discovered that",
    "my love language",
    "i won't shut up about",
    "we'll get along if",
    "the key to my heart",
    "best travel story",
    "change my mind about",
    "two truths and a lie",
    "my greatest strength",
    "i'm convinced that",
    "don't hate me if i",
    "my most controversial opinion",
    "the hallmark of a good relationship",
    "give me travel tips",
)


@dataclass(frozen=True)
class Bounds:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x1, self.y1, self.x2, self.y2


@dataclass(frozen=True)
class UiNode:
    text: str
    content_desc: str
    resource_id: str
    class_name: str
    clickable: bool
    editable: bool
    selected: bool
    bounds: Bounds
    children_text: tuple[str, ...]


@dataclass(frozen=True)
class MatchHit:
    name: str
    preview: str
    section: str
    unread: bool
    is_new: bool
    x: int
    y: int
    bounds: Bounds


@dataclass(frozen=True)
class ProfileParse:
    name: str
    verified: bool
    age: int | None
    job: str
    school: str
    location: str
    gender: str
    height: str
    about: str
    extras: dict[str, str]
    prompts: list[tuple[str, str]]
    photos: list[str]


def parse_bounds(raw: str | None) -> Bounds | None:
    match = BOUNDS_RE.match(raw or "")
    if not match:
        return None
    return Bounds(*(int(p) for p in match.groups()))


def is_hinge_xml(xml: str | None) -> bool:
    blob = xml or ""
    return PACKAGE in blob and "<hierarchy" in blob


def parse_ui_nodes(xml_text: str) -> list[UiNode]:
    start = (xml_text or "").find("<")
    if start == -1:
        return []
    try:
        root = ET.fromstring(xml_text[start:])
    except ET.ParseError:
        return []
    nodes: list[UiNode] = []
    for element in root.iter("node"):
        bounds = parse_bounds(element.attrib.get("bounds", ""))
        if bounds is None:
            continue
        children = tuple(
            (child.attrib.get("text") or "").strip()
            for child in element.iter("node")
            if (child.attrib.get("text") or "").strip()
        )
        nodes.append(
            UiNode(
                text=(element.attrib.get("text") or "").strip(),
                content_desc=(element.attrib.get("content-desc") or "").strip(),
                resource_id=(element.attrib.get("resource-id") or "").strip(),
                class_name=(element.attrib.get("class") or "").split(".")[-1],
                clickable=element.attrib.get("clickable") == "true",
                editable=element.attrib.get("editable") == "true",
                selected=element.attrib.get("selected") == "true",
                bounds=bounds,
                children_text=children,
            )
        )
    return nodes


def find_nodes(
    nodes: list[UiNode],
    *,
    text_contains: str | None = None,
    desc_contains: str | None = None,
    resource_id: str | None = None,
    clickable: bool | None = None,
) -> list[UiNode]:
    out = []
    for node in nodes:
        if text_contains is not None and text_contains.lower() not in node.text.lower():
            continue
        if desc_contains is not None and desc_contains.lower() not in node.content_desc.lower():
            continue
        if resource_id is not None and node.resource_id != resource_id:
            continue
        if clickable is not None and node.clickable != clickable:
            continue
        out.append(node)
    return out


def is_composer_node(node: UiNode) -> bool:
    rid = (node.resource_id or "").lower()
    if "messagecomposition" in rid or "message_composition" in rid:
        return True
    if node.editable or node.class_name == "EditText":
        return True
    return (node.text or "").strip().lower() == "send a message"


def classify_screen(xml: str, *, expect_name: str = "") -> str:
    if not is_hinge_xml(xml):
        return SCREEN_OFF
    nodes = parse_ui_nodes(xml)
    height = max((n.bounds.y2 for n in nodes), default=2400)
    floor = int(height * 0.86)
    selected_nav = ""
    for node in nodes:
        if node.bounds.y1 < floor:
            continue
        label = (node.content_desc or node.text or "").strip().lower()
        if node.selected and label:
            selected_nav = label
            break
    tabs = {(n.text or "").strip().lower() for n in nodes if n.bounds.y1 < 900}
    if "chat" in tabs and "profile" in tabs:
        profile_on = any(
            n.text.strip().lower() == "profile" and n.selected for n in nodes if n.bounds.y1 < 900
        )
        if profile_on or any(PROMPT_RE.match(n.content_desc) for n in nodes):
            return SCREEN_PROFILE
        return SCREEN_CHAT
    if "dating preferences" in " ".join(n.content_desc.lower() for n in nodes):
        return SCREEN_DISCOVER
    if any("like photo" in n.content_desc.lower() or n.content_desc.lower() in {"like", "send like"} for n in nodes):
        return SCREEN_DISCOVER
    if any(n.content_desc.lower().startswith("skip") or n.text.lower().startswith("skip") for n in nodes):
        return SCREEN_DISCOVER
    if selected_nav.startswith("discover") or selected_nav.startswith("explore"):
        return SCREEN_DISCOVER
    headers = " ".join(n.text.lower() for n in nodes)
    if "your turn" in headers or "their turn" in headers:
        return SCREEN_MATCHES
    if selected_nav.startswith("matches"):
        return SCREEN_MATCHES
    if expect_name and any(expect_name.lower() in (n.text or "").lower() for n in nodes):
        return SCREEN_CHAT
    return SCREEN_UNKNOWN


def parse_match_list(xml: str) -> list[MatchHit]:
    if not is_hinge_xml(xml):
        return []
    nodes = parse_ui_nodes(xml)
    kind = classify_screen(xml)
    if kind in {SCREEN_DISCOVER, SCREEN_PROFILE, SCREEN_CHAT}:
        return []
    headers: list[tuple[int, str]] = []
    for node in nodes:
        text = node.text.strip().lower()
        if text.startswith("your turn"):
            headers.append((node.bounds.y1, "your_turn"))
        elif text.startswith("their turn"):
            headers.append((node.bounds.y1, "their_turn"))
        elif text.startswith("hidden"):
            headers.append((node.bounds.y1, "hidden"))
    headers.sort()
    hits: list[MatchHit] = []
    seen: set[str] = set()
    height = max((n.bounds.y2 for n in nodes), default=2400)

    def _section_for(y1: int) -> str:
        section = "unknown"
        for header_y, label in headers:
            if y1 >= header_y:
                section = label
            else:
                break
        return section

    def _add(name: str, preview: str, bounds: Bounds, texts: list[str]) -> None:
        key = name.casefold()
        if key in seen:
            return
        seen.add(key)
        joined = " ".join(texts).lower()
        section = _section_for(bounds.y1)
        is_new = any("start the chat" in t.lower() or t.lower() == "start chat" for t in texts)
        if is_new and section == "unknown":
            section = "your_turn"
        unread = "reply?" in joined or section == "your_turn"
        x, y = bounds.center
        hits.append(
            MatchHit(
                name=name,
                preview=preview,
                section=section,
                unread=unread,
                is_new=is_new,
                x=x,
                y=y,
                bounds=bounds,
            )
        )

    for node in nodes:
        if not node.clickable:
            continue
        row_h = node.bounds.y2 - node.bounds.y1
        if row_h < 36 or row_h > 460:
            continue
        texts = [t for t in node.children_text if t]
        joined = " ".join(texts).lower()
        if any(key in joined for key in ("your turn", "their turn", "over the limit")):
            continue
        nav = (node.content_desc or "").lower()
        if any(key in nav for key in ("discover", "standouts", "likes you", "matches")):
            continue
        if is_composer_node(node):
            continue
        name = ""
        if texts and _plausible_match_name(texts[0].strip()):
            name = texts[0].strip()
        elif _plausible_match_name(node.text):
            name = node.text.strip()
        else:
            desc_name = (node.content_desc or "").split(",")[0].strip()
            if _plausible_match_name(desc_name) and desc_name.lower() not in SKIP_NAMES:
                name = desc_name
        if not name:
            continue
        preview = ""
        if texts:
            preview = next((t for t in texts[1:] if t.strip().casefold() != name.casefold()), "")
        _add(name, preview, node.bounds, texts or [name, preview])

    # Compose dumps sometimes leave names as plain TextViews with no clickable parent.
    for node in nodes:
        name = node.text.strip()
        if not _plausible_match_name(name) or len(name.split()) >= 4:
            continue
        if name.casefold() in seen:
            continue
        if node.bounds.y1 < 150 or node.bounds.y1 > int(height * 0.90):
            continue
        if any(abs(node.bounds.y1 - hit.bounds.y1) < 40 for hit in hits):
            continue
        if any(
            hit.bounds.y1 <= node.bounds.y1 <= hit.bounds.y2
            and hit.bounds.x1 <= node.bounds.x1 <= hit.bounds.x2
            for hit in hits
        ):
            continue
        if any(
            0 <= node.bounds.y1 - hit.bounds.y2 <= 90 and abs(node.bounds.x1 - hit.bounds.x1) < 140
            for hit in hits
        ):
            continue
        preview = ""
        for other in nodes:
            if other is node or not other.text:
                continue
            if 0 <= other.bounds.y1 - node.bounds.y2 <= 80 and abs(other.bounds.x1 - node.bounds.x1) < 80:
                preview = other.text.strip()
                break
        _add(name, preview, node.bounds, [name, preview])
    return hits


def _plausible_match_name(name: str) -> bool:
    cand = (name or "").strip()
    if not cand or cand.lower() in SKIP_NAMES:
        return False
    if re.match(r"^(your turn|their turn|hidden)\b", cand, re.I):
        return False
    if len(cand) < 2 or len(cand) > 48:
        return False
    if not re.search(r"[A-Za-z]", cand):
        return False
    if re.search(r"[!?.,:;]", cand):
        return False
    if len(cand.split()) >= 3:
        return False
    if len(cand.split()) >= 5 and any(ch in cand for ch in "?.,"):
        return False
    return True


def parse_profile(xml: str) -> ProfileParse:
    nodes = parse_ui_nodes(xml)
    name = ""
    verified = False
    age = None
    extras: dict[str, str] = {}
    prompts: list[tuple[str, str]] = []
    photos: list[str] = []
    pending_label = ""
    for node in nodes:
        desc = node.content_desc
        if re.search(r",\s*verified\s*$", desc, re.I):
            name = re.sub(r",\s*verified\s*$", "", desc, flags=re.I).strip() or name
            verified = True
        if desc.lower() in {"verified", "selfie verified"}:
            verified = True
        photo = PHOTO_RE.match(desc)
        if photo:
            photos.append(photo.group(1).strip())
        prompt = PROMPT_RE.match(desc)
        if prompt:
            prompts.append((prompt.group(1).strip().rstrip("."), prompt.group(2).strip()))
        label = desc.strip().lower()
        if label in BASIC_LABELS or label in LABEL_ALIASES:
            pending_label = LABEL_ALIASES.get(label, label)
            continue
        if pending_label and node.text:
            extras[pending_label] = node.text.strip()
            if pending_label == "age":
                try:
                    age = int(node.text.strip())
                except ValueError:
                    pass
            pending_label = ""
        if not name and node.text and node.bounds.y1 < 400 and _plausible_match_name(node.text):
            name = node.text.strip()
    for extra_prompt in _prompts_from_visible_text(nodes):
        if extra_prompt not in prompts and extra_prompt[0].casefold() not in {
            q.casefold() for q, _a in prompts
        }:
            prompts.append(extra_prompt)
    if not name and photos:
        name = photos[0]
    about_bits = [f"{q}: {a}" for q, a in prompts[:3]]
    return ProfileParse(
        name=name,
        verified=verified,
        age=age,
        job=extras.get("job", ""),
        school=extras.get("school") or extras.get("education", ""),
        location=extras.get("location") or extras.get("hometown", ""),
        gender=extras.get("gender", ""),
        height=extras.get("height", ""),
        about=" · ".join(about_bits),
        extras=extras,
        prompts=prompts,
        photos=photos,
    )


def merge_profiles(*parts: ProfileParse) -> ProfileParse:
    name = next((p.name for p in parts if p.name), "")
    extras: dict[str, str] = {}
    prompts: list[tuple[str, str]] = []
    photos: list[str] = []
    seen_q: set[str] = set()
    age = None
    verified = False
    for part in parts:
        verified = verified or part.verified
        if part.age is not None:
            age = part.age
        extras.update({k: v for k, v in part.extras.items() if v})
        for question, answer in part.prompts:
            key = question.casefold()
            if key in seen_q:
                continue
            seen_q.add(key)
            prompts.append((question, answer))
        for photo in part.photos:
            if photo not in photos:
                photos.append(photo)
        if not name and part.name:
            name = part.name
    about_bits = [f"{q}: {a}" for q, a in prompts[:6]]
    return ProfileParse(
        name=name,
        verified=verified,
        age=age,
        job=extras.get("job", ""),
        school=extras.get("school") or extras.get("education", ""),
        location=extras.get("location") or extras.get("hometown", ""),
        gender=extras.get("gender", ""),
        height=extras.get("height", ""),
        about=" · ".join(about_bits),
        extras=extras,
        prompts=prompts,
        photos=photos,
    )


def profile_photo_boxes(xml: str) -> list[Bounds]:
    boxes: list[Bounds] = []
    seen: set[tuple[int, int, int, int]] = set()
    for node in parse_ui_nodes(xml):
        if not PHOTO_RE.match(node.content_desc):
            continue
        width = node.bounds.x2 - node.bounds.x1
        height = node.bounds.y2 - node.bounds.y1
        if width < 240 or height < 180:
            continue
        key = node.bounds.as_tuple()
        if key in seen:
            continue
        seen.add(key)
        boxes.append(node.bounds)
    return boxes


_THREAD_SKIP_RE = re.compile(
    r"^(mon|tue|wed|thu|fri|sat|sun)\b|"
    r"^\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b|"
    r"^\d{1,2}:\d{2}\b|"
    r"^double tap|"
    r"^today$|^yesterday$|"
    r"^get notifications\b|"
    r"^enable for\b|"
    r"^timing is everything\b|"
    r"turn on notifications",
    re.I,
)


def _clean_thread_body(body: str) -> str:
    body = re.sub(r"\s*You liked this message\.?\s*$", "", (body or "").strip(), flags=re.I)
    return re.sub(r"\s+", " ", body).strip()


def _thread_side(node: UiNode, *, name: str, width: int, sender: str = "") -> str:
    who = (sender or "").strip()
    if who.lower() == "you":
        return "you"
    if name and who and who.casefold() == name.casefold():
        return "them"
    if who and who.lower() != "you":
        return "them"
    mid = max(width, 1) * 0.55
    return "you" if node.bounds.center[0] >= mid else "them"


def parse_open_thread(xml: str) -> tuple[str, list[tuple[str, str]]]:
    """Oldest message first. Sides come from You:/Name: labels or left/right bubbles."""
    nodes = parse_ui_nodes(xml)
    name = ""
    for node in nodes:
        if node.bounds.y1 > 500:
            continue
        desc = node.content_desc
        if re.search(r",\s*verified\s*$", desc, re.I):
            name = re.sub(r",\s*verified\s*$", "", desc, flags=re.I).strip()
            break
        if node.text and _plausible_match_name(node.text) and node.text.lower() not in {"chat", "profile"}:
            name = node.text.strip()
    width = max((n.bounds.x2 for n in nodes), default=1080)
    composer_y = min((n.bounds.y1 for n in nodes if is_composer_node(n)), default=10_000)
    found: list[tuple[int, int, str, str]] = []
    seen_body: set[str] = set()

    def _add(node: UiNode, side: str, body: str) -> None:
        body = _clean_thread_body(body)
        if not body:
            return
        key = body.casefold()
        if key in seen_body:
            return
        if name and key == name.casefold():
            return
        seen_body.add(key)
        found.append((node.bounds.y1, node.bounds.x1, side, body))

    for node in nodes:
        if is_composer_node(node) or node.bounds.y1 < 380 or node.bounds.y2 >= composer_y:
            continue
        desc = node.content_desc.strip()
        if not desc or PROMPT_RE.match(desc) or PHOTO_RE.match(desc):
            continue
        if _THREAD_SKIP_RE.search(desc):
            continue
        if desc.lower() in BASIC_LABELS or desc.lower() in CHROME_TEXT:
            continue
        match = MESSAGE_RE.match(desc)
        if not match:
            continue
        sender = match.group(1).strip()
        _add(node, _thread_side(node, name=name, width=width, sender=sender), match.group(2))

    pending_prompt = ""
    pending_node: UiNode | None = None
    for node in sorted(
        (n for n in nodes if n.text.strip() and not is_composer_node(n)),
        key=lambda n: (n.bounds.y1, n.bounds.x1),
    ):
        text = node.text.strip()
        if node.bounds.y1 < 380 or node.bounds.y2 >= composer_y:
            continue
        if text.lower() in CHROME_TEXT or text.lower() in {"chat", "profile"}:
            continue
        if name and text.casefold() == name.casefold():
            continue
        if _THREAD_SKIP_RE.search(text):
            continue
        if text.lower().startswith("get notifications") or text.lower().startswith("enable for"):
            continue
        if _looks_like_prompt(text):
            pending_prompt = text
            pending_node = node
            continue
        if pending_prompt:
            _add(
                pending_node or node,
                "them",
                f"{pending_prompt} {text}",
            )
            pending_prompt = ""
            pending_node = None
            continue
        if MESSAGE_RE.match(node.content_desc or ""):
            continue
        _add(node, _thread_side(node, name=name, width=width), text)
    if pending_prompt and pending_node is not None:
        _add(pending_node, "them", pending_prompt)
    found.sort(key=lambda row: (row[0], row[1]))
    return name, [(side, body) for _y, _x, side, body in found]


def _bounds_contain(outer: Bounds, inner: Bounds) -> bool:
    cx, cy = inner.center
    return outer.x1 <= cx <= outer.x2 and outer.y1 <= cy <= outer.y2


def find_nav_point(xml: str, label: str) -> tuple[int, int] | None:
    want = label.strip().lower()
    nodes = parse_ui_nodes(xml)
    extra = {"discover", "standouts", "likes you", "matches", "profile hub"}
    extra.discard(want)
    labeled = [
        n
        for n in nodes
        if want in f"{n.content_desc} {n.text} {' '.join(n.children_text)}".lower()
    ]
    candidates: list[UiNode] = []
    for node in nodes:
        blob = f"{node.content_desc} {node.text} {' '.join(node.children_text)}".lower()
        if want in blob and node.clickable:
            kids = " ".join(node.children_text).lower()
            width = node.bounds.x2 - node.bounds.x1
            if width > 400 and any(item in kids for item in extra):
                continue
            candidates.append(node)
    if not candidates:
        clickables = [n for n in nodes if n.clickable]
        for label_node in labeled:
            for click in clickables:
                width = click.bounds.x2 - click.bounds.x1
                if width > 900:
                    continue
                if _bounds_contain(click.bounds, label_node.bounds):
                    candidates.append(click)
    if not candidates:
        return None
    candidates.sort(key=lambda n: (n.bounds.x2 - n.bounds.x1) * (n.bounds.y2 - n.bounds.y1))
    return candidates[0].bounds.center


def find_like_photo(xml: str) -> tuple[int, int] | None:
    photos, prompts, generic = [], [], []
    for node in parse_ui_nodes(xml):
        if not node.clickable:
            continue
        desc = node.content_desc.lower().strip()
        text = node.text.lower().strip()
        blob = f"{desc} {text}"
        if "like photo" in blob or desc.startswith("like photo"):
            (prompts if "prompt" in blob else photos).append(node)
        elif desc in {"like", "send like"} or text == "like":
            generic.append(node)
    hit = photos or prompts or generic
    return hit[0].bounds.center if hit else None


def find_skip(xml: str) -> tuple[int, int] | None:
    for node in parse_ui_nodes(xml):
        if not node.clickable:
            continue
        desc = node.content_desc.lower().strip()
        text = node.text.lower().strip()
        if desc.startswith("skip") or text.startswith("skip"):
            return node.bounds.center
    return None


def _center_from_u2_bounds(info: dict) -> tuple[int, int] | None:
    bounds = info.get("bounds") or {}
    try:
        return (
            (int(bounds["left"]) + int(bounds["right"])) // 2,
            (int(bounds["top"]) + int(bounds["bottom"])) // 2,
        )
    except (KeyError, TypeError, ValueError):
        return None


def find_like_on_device(device) -> tuple[int, int] | None:
    """Compose cards often omit Like from compressed dumps; query u2 instead."""
    try:
        obj = device(descriptionStartsWith="Like photo")
        if obj.exists(timeout=0.6):
            return _center_from_u2_bounds(obj.info)
    except Exception:
        return None
    return None


def find_skip_on_device(device) -> tuple[int, int] | None:
    try:
        obj = device(descriptionStartsWith="Skip")
        if obj.exists(timeout=0.6):
            desc = str((obj.info or {}).get("contentDescription") or "")
            if desc.lower().startswith("skip"):
                return _center_from_u2_bounds(obj.info)
    except Exception:
        return None
    return None


def discover_card_name_on_device(device) -> str:
    try:
        obj = device(descriptionStartsWith="Skip")
        if obj.exists(timeout=0.4):
            desc = str((obj.info or {}).get("contentDescription") or "").strip()
            if desc.lower().startswith("skip "):
                return desc[5:].strip()
    except Exception:
        return ""
    return ""


def find_send_like(xml: str) -> tuple[int, int] | None:
    for node in parse_ui_nodes(xml):
        blob = f"{node.content_desc} {node.text}".lower()
        if "send like" in blob and node.clickable:
            return node.bounds.center
    return None


def find_composer(xml: str) -> tuple[int, int] | None:
    for node in parse_ui_nodes(xml):
        if node.resource_id == RESOURCE_COMPOSER or is_composer_node(node):
            return node.bounds.center
    return None


def find_send(xml: str) -> tuple[int, int] | None:
    for node in parse_ui_nodes(xml):
        if node.resource_id == RESOURCE_SEND or (
            node.clickable and node.content_desc.lower() in {"send", "send message"}
        ):
            return node.bounds.center
    return None


def _looks_like_prompt(text: str) -> bool:
    blob = (text or "").strip().lower()
    if any(blob.startswith(stem) or stem in blob for stem in PROMPT_STEMS):
        return True
    words = blob.split()
    return len(words) <= 8 and blob.endswith((" for", " is", " if", " about"))


def _prompts_from_visible_text(nodes: list[UiNode]) -> list[tuple[str, str]]:
    ordered = sorted(
        (n for n in nodes if n.text.strip() and n.text.strip().lower() not in CHROME_TEXT),
        key=lambda n: (n.bounds.y1, n.bounds.x1),
    )
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for idx, node in enumerate(ordered):
        text = node.text.strip()
        if not _looks_like_prompt(text):
            continue
        answer = ""
        if idx + 1 < len(ordered):
            nxt = ordered[idx + 1].text.strip()
            if nxt and nxt.lower() not in CHROME_TEXT and not _looks_like_prompt(nxt):
                answer = nxt
        key = f"{text.casefold()}|{answer.casefold()}"
        if key in seen:
            continue
        seen.add(key)
        out.append((text, answer))
    return out


def find_tab(xml: str, label: str) -> tuple[int, int] | None:
    want = label.strip().lower()
    candidates = [
        n
        for n in parse_ui_nodes(xml)
        if n.bounds.y1 < 1400
        and (n.text.strip().lower() == want or n.content_desc.strip().lower() == want)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda n: n.bounds.x1, reverse=want == "profile")
    return candidates[0].bounds.center


def discover_card_name(xml: str) -> str:
    for node in parse_ui_nodes(xml):
        if PHOTO_RE.match(node.content_desc):
            return PHOTO_RE.match(node.content_desc).group(1).strip()
        if node.bounds.y1 < 1000 and _plausible_match_name(node.text) and node.text.lower() not in CHROME_TEXT:
            return node.text.strip()
    return ""


def out_of_cards(xml: str) -> bool:
    blob = (xml or "").lower()
    return any(
        phrase in blob
        for phrase in (
            "you're out of profiles",
            "you are out of profiles",
            "no more people",
            "we're out of people",
            "out of likes",
            "like limit",
        )
    )


def like_limit_reached(xml: str) -> bool:
    blob = (xml or "").lower()
    return "out of likes" in blob or "like limit" in blob
