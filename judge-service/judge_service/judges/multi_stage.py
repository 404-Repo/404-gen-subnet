"""Multi-stage duel judge: decide which of two 3D models better matches a reference image.

The judge runs four stages, each gating on the previous one. A stage that produces a
directional verdict short-circuits the rest of the pipeline; ambiguous outcomes fall
through to the next stage.

    Stage 1 — Prompt match (always runs).
        4 front-hemisphere angles, each evaluated AB and BA = 8 VLM calls. A weighted,
        split-aware aggregator either declares a winner or routes to Stage 2.

    Stage 2 — Three independent sub-judges, run in parallel, then combined:
        * Best view: DINOv3 picks the highest-similarity rendered view per side; the
          VLM compares those two views (AB+BA = 2 calls).
        * Artifact compare: a 2x2 grid render per side, VLM rates rendering quality
          (AB+BA = 2 calls).
        * Checklist verify: VLM extracts a feature checklist from the prompt, then
          verifies each grid (1 decompose + 2 verify = 3 calls).
        Strong-voter override fires on a single very confident sub-judge. Otherwise a
        2-of-3 consensus picks the winner; ties route to Stage 3.

    Stage 3 — Gray-background rescue (gated).
        Some pathological renders look reasonable on white but reveal flaws on a neutral
        gray background. A pixel-statistics gate fires only on pale + grayish foregrounds;
        when it fires, 2 VLM calls on the gray front views can override the Stage 2 draw.

    Stage 4 — Side guard (always runs).
        4 side angles per side × OK/GARBAGE classification = 8 VLM calls. K-of-4 garbage
        votes flag a side; step-down rules demote a winning-side-flagged-garbage to draw.

All renders + DINOv3 embeddings are produced upstream and live under the `views/` CDN
prefix from `subnet_common.embeddings` / `subnet_common.render`. The judge fetches
`embeddings.npz` (for Stage 2A's best-view selection) and the two gray-front PNGs (for
Stage 3's foreground gate) over HTTP; every other image goes to the VLM as a URL.

The numeric detail from each call (penalties, similarities, checklist gaps, side-guard
verdicts) is returned alongside the final winner so each duel record carries its full
trace. VLM `issues` text is intentionally dropped to keep persisted match reports compact.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import struct
import zlib
from collections import Counter
from typing import Any, Literal, cast

import httpx
import numpy as np
from loguru import logger
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError
from subnet_common.competition.generations import GenerationResult
from subnet_common.competition.match_report import DuelWinner


# VLM model id served by the operator's vLLM endpoint. Default is the Z.ai
# (Zhipu AI) GLM-4.6V-Flash; operators are responsible for accepting that model's
# license terms when running their endpoint. See NOTICE.md for attribution.
MODEL = "glm-4.6v-flash"

# Stage 1 — front prompt-match aggregator.
S1_DRAW_THRESHOLD = 0.7
S1_BOTH_BAD_AVG = 8.0
S1_BOTH_BAD_MAX_DIFF = max(1.5, S1_DRAW_THRESHOLD)
S1_SPLIT_CONSISTENCY_THRESH = 0.65
S1_SPLIT_MIN_SIGNED = 3

# Stage 1 angles: subset of WHITE_VIEWS used for front prompt-match. (theta, phi, weight, label).
# Four near-front angles with uniform weights; pure (0,0) is intentionally absent in
# favour of vertical diversity (front_above + front_below).
S1_ANGLES: list[tuple[int, int, float, str]] = [
    (30, 0, 1.0, "front_left"),
    (330, 0, 1.0, "front_right"),
    (0, 15, 1.0, "front_below"),
    (0, -30, 1.0, "front_above"),
]
S1_ANGLE_DESC: dict[str, str] = {
    "front_left": "slightly left of the front",
    "front_right": "slightly right of the front",
    "front_below": "the front, from slightly below",
    "front_above": "the front, from slightly above",
}

# Stage 2 — consensus thresholds.
S2_BV_MIN_GAP = 1.0
S2_AC_MIN_GAP = 1.0
S2_CL_MIN_GAP = 2
S2_MIN_CONSENSUS = 2
S2_STRONG_BV_GAP = 2.0
S2_STRONG_CL_GAP = 6
S2_CHECKLIST_GAP_THRESHOLD = 2
S2_CHECKLIST_MAIN_BONUS = 3
S2_CHECKLIST_MATCH_SCORES: dict[str, int] = {"yes": 2, "partial": 1, "no": 0}
S2_CHECKLIST_MIN_FEATURES = 4
S2_CHECKLIST_MAX_FEATURES = 7

# Stage 3 — gray-bg rescue.
S3_BACKGROUND_RGB: tuple[int, int, int] = (128, 128, 128)
S3_BACKGROUND_TOLERANCE = 18
S3_SAMPLE_STEP = 4
S3_PALE_LUMA_MIN = 175.0
S3_PALE_SAT_MAX = 0.35
S3_GRAYISH_SAT_MAX = 0.28
S3_PALE_FRACTION_MIN = 0.10
S3_GRAYISH_FRACTION_MIN = 0.60
S3_GRAY_DIFF_THRESHOLD = 2.0
S3_FRONT_LABEL = "front_left"  # which gray view to use; must be present in GRAY_VIEWS
S3_FRONT_DESC = "slightly left of the front"
S3_VLM_MAX_RETRIES = 5
S3_VLM_MAX_TOKENS = 1024

# Stage 4 — per-angle side guard.
S4_K_THRESHOLD = 3
S4_VLM_MAX_TOKENS = 220
S4_VLM_MAX_RETRIES = 4
S4_FRONT_LABEL = "front_left"
S4_SIDE_ANGLES: list[tuple[str, str]] = [
    ("right", "the right side"),
    ("back", "the back"),
    ("left", "the left side"),
    ("top_down", "directly above (top-down)"),
]


# JSON robustness
JSON_FAILED_MARKER = "JSON_PARSE_FAILED"
JSON_DEFAULT_MAX_RETRIES = 5
JSON_DEFAULT_BACKOFF_BASE = 0.5

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_OUTER_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _candidate_json_strings(text: str) -> list[str]:
    if not text:
        return []
    candidates: list[str] = [text.strip()]
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    brace = _OUTER_BRACE_RE.search(text)
    if brace:
        candidates.append(brace.group(0).strip())
    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _coerce_ints(obj: Any, model_cls: type[BaseModel]) -> Any:
    if not isinstance(obj, dict):
        return obj
    for name, info in model_cls.model_fields.items():
        if name not in obj:
            continue
        if info.annotation is int and not isinstance(obj[name], int):
            try:
                obj[name] = int(round(float(obj[name])))
            except (TypeError, ValueError):
                pass
    return obj


def _parse_or_repair(text: str, model_cls: type[BaseModel]) -> BaseModel | None:
    if not text:
        return None
    try:
        return model_cls.model_validate_json(text)
    except ValidationError:
        pass
    for cand in _candidate_json_strings(text):
        try:
            return model_cls.model_validate_json(cand)
        except ValidationError:
            pass
        try:
            obj = json.loads(cand)
            obj = _coerce_ints(obj, model_cls)
            return model_cls.model_validate(obj)
        except (json.JSONDecodeError, ValidationError):
            pass
        repaired = _TRAILING_COMMA_RE.sub(r"\1", cand)
        try:
            obj = json.loads(repaired)
            obj = _coerce_ints(obj, model_cls)
            return model_cls.model_validate(obj)
        except (json.JSONDecodeError, ValidationError):
            pass
    return None


# Schemas


class PenaltyResponse(BaseModel):
    """Shared schema for prompt-match / best_view / artifact_compare / gray-rescue calls."""

    penalty_1: int
    penalty_2: int
    issues: str


class ChecklistItem(BaseModel):
    description: str
    category: Literal["count", "shape", "presence", "material_color"]


class PromptChecklist(BaseModel):
    main_object: str
    features: list[ChecklistItem] = Field(
        min_length=S2_CHECKLIST_MIN_FEATURES,
        max_length=S2_CHECKLIST_MAX_FEATURES,
    )


class VerifyItem(BaseModel):
    feature: str
    match: Literal["yes", "partial", "no"]
    note: str


class VerificationResult(BaseModel):
    checks: list[VerifyItem]
    main_object_correct: bool


class SideGuardVerdict(BaseModel):
    """Stage 4 per-angle verdict. JSON schema disciplines the model away from <think>...</think>
    leakage that would otherwise consume the small `S4_VLM_MAX_TOKENS` budget."""

    verdict: Literal["ok", "garbage"]
    reason: str


class IssuesSummary(BaseModel):
    """Final summary call: a short human-readable comparison of the two models' issues."""

    issues: str


def _neutral_penalty() -> PenaltyResponse:
    return PenaltyResponse(penalty_1=5, penalty_2=5, issues=JSON_FAILED_MARKER)


def _neutral_issues() -> IssuesSummary:
    """Fallback when the explain call fails — empty issues, never fails the duel."""
    return IssuesSummary(issues="")


# VLM prompts. Phrasing is load-bearing — penalty distributions and verdict rates were
# tuned against these strings, so edits should ride with a re-tune of the thresholds above.

S1_S2BV_S2AC_SYSTEM_PROMPT = (
    "You are a specialized 3D model evaluation system.\n"
    "Analyze visual quality and prompt adherence with expert precision.\n"
    "Always respond with valid JSON only."
)

S1_PROMPT_MATCH_USER = (
    "You see two 3D models rendered from {angle_desc}.\n"
    "The reference image shows the target object.\n\n"
    "Which model is a more faithful 3D reproduction of the reference?\n\n"
    "Penalty 0-10:\n"
    "0 = Perfect match to reference\n"
    "3 = Minor issues (slight shape differences, missing small details)\n"
    "5 = Moderate issues (wrong style, significant details missing)\n"
    "7 = Major issues (wrong category but related, e.g. chair vs stool)\n"
    "10 = Completely wrong object\n\n"
    'Output: {{"penalty_1": <0-10>, "penalty_2": <0-10>, "issues": "<brief>"}}'
)

S2BV_USER_PROMPT = (
    "Does each 3D model match the image prompt?\n\n"
    "Each model is shown from its single best prompt-matching angle.\n\n"
    "Penalty 0-10:\n"
    "0 = Perfect match\n"
    "3 = Minor issues (slight shape differences, missing small details)\n"
    "5 = Moderate issues (wrong style, significant details missing)\n"
    "7 = Major issues (wrong category but related, e.g. chair vs stool)\n"
    "10 = Completely wrong object\n\n"
    'Output: {"penalty_1": <0-10>, "penalty_2": <0-10>, "issues": "<brief>"}'
)

S2AC_USER_PROMPT = (
    "Rate each 3D model's rendering quality against the image prompt.\n\n"
    "Focus on these quality dimensions:\n\n"
    "1. BOUNDING BOX: Does the model include an unrelated box-like enclosure,\n"
    "   room walls, or floor plane that is NOT part of the object itself?\n"
    "   Bases or platforms that match the prompt context (e.g. a ground plane\n"
    "   for a character standing on ground) are NOT penalties.\n"
    "   Unrelated enclosures are a major flaw (penalty 5-7).\n\n"
    "2. COLOR & MATERIAL ACCURACY: Does each model reproduce the distinctive\n"
    "   colors and materials from the prompt? A missing or wrong color on a\n"
    "   key part (e.g. a red element appearing gray) is a clear flaw (penalty 3-5).\n\n"
    "3. SURFACE QUALITY: Are there clearly visible texture artifacts — heavy\n"
    "   noise, large speckles, or obvious seams — that make one model look\n"
    "   significantly worse? Minor differences in texture sharpness are normal\n"
    "   and should not affect the penalty (penalty only if clearly worse: 2-3).\n\n"
    "4. GEOMETRIC DETAIL: Missing fine structural details like handles, edges,\n"
    "   thin features, or ornamental elements (penalty 2-4).\n\n"
    "Penalty 0-10:\n"
    "0 = Excellent rendering, faithful to the prompt on all dimensions\n"
    "3 = Minor issues on one dimension\n"
    "5 = Clear flaw on one or more dimensions\n"
    "7 = Multiple serious flaws\n"
    "10 = Completely wrong\n\n"
    'Output: {"penalty_1": <0-10>, "penalty_2": <0-10>, "issues": "<brief comparison>"}'
)

S2CL_DECOMPOSE_SYSTEM_PROMPT = (
    "You are a visual analyst for 3D model evaluation.\n\n"
    "Analyze a reference image and extract a structured checklist of concrete,\n"
    "verifiable features that a matching 3D model must have.\n\n"
    "Do not output chain-of-thought.\n"
    "Always respond with valid JSON only."
)

S2CL_DECOMPOSE_USER_PROMPT = (
    "Look at this reference image and create a checklist of concrete features\n"
    "a matching 3D model must have.\n\n"
    "Rules:\n"
    '- Identify the main object first (e.g. "wooden chair", "red sports car")\n'
    "- List 4-7 specific, verifiable features\n"
    '- Prefer countable features: "4 legs", "2 handles", "3 wheels"\n'
    '- Include shape features: "round body", "tapered legs", "curved back"\n'
    '- Include distinguishing details: "open top", "visible wheels", "pointed ears"\n'
    '- Include material/color ONLY when distinctive: "metallic surface", "bright red"\n'
    "- Every feature must be checkable from a multi-view 3D render\n"
    "- Do NOT include background, lighting, or rendering-style features\n"
    '- Do NOT include vague features like "good quality" or "realistic"\n\n'
    "Output JSON:\n"
    '{"main_object": "<2-4 word label>", "features": ['
    '{"description": "<short concrete feature>", '
    '"category": "count" | "shape" | "presence" | "material_color"}]}'
)

S2CL_VERIFY_SYSTEM_PROMPT = (
    "You are verifying whether a 3D model matches a reference checklist.\n\n"
    "You see four different views of a single 3D model (2x2 grid). Use all\n"
    "four views to assess each checklist item.\n\n"
    "Be strict:\n"
    '- "yes"     = clearly present and correct across the views\n'
    '- "partial" = present but wrong in some way (wrong count, shape, color)\n'
    '- "no"      = absent or completely wrong\n\n'
    "Always respond with valid JSON only."
)

S4_SYSTEM_PROMPT = (
    "You are a precise visual consistency checker for 3D renders.\n"
    "Answer briefly. Do not include chain-of-thought. Do not use <think> tags.\n"
    "Always respond with valid JSON only."
)

S4_PER_ANGLE_PROMPT = (
    "The first image is a reference view of a generated 3D model.\n"
    "The second image is the SAME 3D model viewed from {angle_desc}.\n\n"
    "Both images are renders of the same 3D scene from different camera angles, so\n"
    "silhouettes, visible faces, and detail levels will naturally differ. Your job\n"
    "is to detect only severe gaming patterns where the model is essentially missing\n"
    "or replaced from this viewing angle.\n\n"
    "Mark GARBAGE ONLY if at least one of the following is clearly true:\n"
    "  (a) the {angle_desc} view shows a flat, paper-thin, or empty silhouette that\n"
    "      is incompatible with a solid 3D version of what the front view depicts\n"
    "      (the back / side has not been modelled at all), OR\n"
    "  (b) the {angle_desc} view shows a mostly empty frame with the object missing,\n"
    "      cut away, or replaced by unrelated geometry (large boxes, walls,\n"
    "      enclosures, slabs, scaffolding, debris that are clearly not part of the\n"
    "      object), OR\n"
    "  (c) the {angle_desc} view shows a completely different and unrelated object\n"
    "      from the one in the front view.\n\n"
    "Mark OK in all other cases. In particular, the following are NOT garbage:\n"
    "  - The silhouette looks different because of the rotated camera angle\n"
    "    (e.g. a tree trunk seen end-on shows no branches; a flat panel seen edge-on\n"
    "    looks thin; a long object seen from the side looks short).\n"
    "  - The non-front view shows interior detail, contents, or geometry that is\n"
    "    naturally hidden from the front (rooms, vehicles, open furniture).\n"
    "  - The non-front view has lower detail, simpler texturing, or fewer features\n"
    "    than the front (3D models often have less detail on hidden faces).\n"
    "  - Multi-object scenes where the parts are intentionally separate (a chess\n"
    "    set, a dining table with chairs, a city block with multiple buildings).\n"
    "  - Stylization, mediocre quality, low polygon count, or odd proportions that\n"
    "    are present in BOTH views.\n\n"
    'Output JSON: {{"verdict": "ok" | "garbage", '
    '"reason": "<one short sentence saying which condition (a/b/c) triggered GARBAGE, '
    'or what makes the {angle_desc} view consistent with the front>"}}'
)


EXPLAIN_SYSTEM_PROMPT = (
    "You are a specialized 3D model evaluation system.\n"
    "Compare two 3D models against a reference image and summarize their main quality "
    "issues briefly. No chain-of-thought.\n"
    "Always respond with valid JSON only."
)

EXPLAIN_USER_PROMPT = (
    "You see a reference image and two 3D models (each shown as a 4-view 2x2 grid).\n\n"
    "Identify the most important rendering and prompt-match issues for each model. "
    "Focus on what is clearly wrong vs the reference: missing features, wrong colors, "
    "geometric distortions, surface artifacts. Skip minor differences.\n\n"
    'Output JSON: {"issues": "<one or two short sentences comparing the two models\' issues>"}'
)


def _s2cl_build_verify_user_prompt(checklist: PromptChecklist) -> str:
    lines = [f"  {i + 1}. [{f.category}] {f.description}" for i, f in enumerate(checklist.features)]
    checks_hint = ", ".join(
        f'{{"feature": "{f.description}", "match": "yes"|"partial"|"no", "note": "<brief>"}}'
        for f in checklist.features
    )
    return (
        f"Below is the reference image and a checklist.\n\n"
        f"Main object: {checklist.main_object}\n"
        f"Features to check:\n" + "\n".join(lines) + "\n\n"
        "Look at the four views of this 3D model and verify each feature.\n\n"
        "Rules:\n"
        "- Check each feature independently using all four views\n"
        '- "yes" = clearly present and correct\n'
        '- "partial" = present but wrong in some way\n'
        '- "no" = absent or completely wrong\n'
        '- Ambiguous → "partial", not "yes"\n'
        "- Note: 3-8 words explaining your verdict\n\n"
        "Output JSON:\n"
        f'{{"checks": [{checks_hint}], "main_object_correct": true | false}}'
    )


# safe_chat_json — JSON-schema chat with multi-tier recovery + retries (AsyncOpenAI)


async def _safe_chat_json(
    client: AsyncOpenAI,
    messages: list[dict],
    response_model: type[BaseModel],
    *,
    label: str,
    seed: int = 42,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    max_retries: int = JSON_DEFAULT_MAX_RETRIES,
    on_failure: BaseModel | None = None,
) -> BaseModel:
    last_err: Exception | None = None
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": f"{label}-response",
            "schema": response_model.model_json_schema(),
        },
    }
    for attempt in range(max_retries):
        temp = temperature if attempt == 0 else 0.1 + 0.05 * attempt
        attempt_seed = seed + attempt + 100
        try:
            completion = await client.chat.completions.create(  # type: ignore[call-overload]
                model=MODEL,
                messages=messages,
                temperature=temp,
                max_tokens=max_tokens,
                response_format=response_format,
                seed=attempt_seed,
            )
            text = completion.choices[0].message.content or ""
            parsed = _parse_or_repair(text, response_model)
            if parsed is not None:
                if attempt > 0:
                    logger.info(f"{label}: JSON parsed on attempt {attempt + 1}/{max_retries}")
                return parsed
            last_err = ValueError(f"Unparseable VLM response: {text[:200]!r}")
        except (ValidationError, ValueError) as exc:
            last_err = exc
        except Exception as exc:
            last_err = exc

        logger.warning(
            f"{label}: attempt {attempt + 1}/{max_retries} failed: " f"{type(last_err).__name__}: {str(last_err)[:160]}"
        )
        if attempt + 1 < max_retries:
            await asyncio.sleep(JSON_DEFAULT_BACKOFF_BASE * (attempt + 1))

    if on_failure is not None:
        logger.error(
            f"{label}: all {max_retries} attempts failed; falling back to neutral. "
            f"Last err: {type(last_err).__name__ if last_err else 'unknown'}"
        )
        return on_failure
    raise last_err or RuntimeError(f"{label}: all retries failed")


# Asset URL builders + HTTP fetchers


def _white_url(views_prefix: str, name: str) -> str:
    return f"{views_prefix}/white/{name}.png"


def _gray_url(views_prefix: str, name: str) -> str:
    return f"{views_prefix}/gray/{name}.png"


def _grid_url(views_prefix: str) -> str:
    return f"{views_prefix}/grid.png"


def _embeddings_url(views_prefix: str) -> str:
    return f"{views_prefix}/embeddings.npz"


async def _fetch_bytes(http: httpx.AsyncClient, url: str, timeout: float = 60.0) -> bytes | None:
    try:
        response = await http.get(url, timeout=httpx.Timeout(timeout, connect=10))
        response.raise_for_status()
        return response.content
    except Exception as e:
        logger.warning(f"fetch failed {url}: {e}")
        return None


async def _load_embeddings(http: httpx.AsyncClient, views_prefix: str) -> dict[str, np.ndarray] | None:
    """Download embeddings.npz and return {key: 1-D embedding array}.

    Producer-side keys are `prompt` + `view_<name>` for the 8 white views.
    """
    data = await _fetch_bytes(http, _embeddings_url(views_prefix))
    if data is None:
        return None
    try:
        with np.load(io.BytesIO(data)) as npz:
            return {k: npz[k] for k in npz.files}
    except Exception as e:
        logger.warning(f"embeddings.npz parse failed for {views_prefix}: {e}")
        return None


# PNG reader (kept inline so the judge has no native deps for foreground stats)


def _read_png_rgb(data: bytes) -> tuple[int, int, int, list[list[int]]]:
    """Decode an 8-bit RGB/RGBA PNG without external deps. Returns (w, h, bpp, rows)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not a PNG")

    pos = 8
    width = height = bit_depth = color_type = None
    compressed = b""
    while pos < len(data):
        chunk_len = struct.unpack(">I", data[pos : pos + 4])[0]
        pos += 4
        chunk_type = data[pos : pos + 4]
        pos += 4
        chunk = data[pos : pos + chunk_len]
        pos += chunk_len + 4
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, _ = struct.unpack(">IIBBBBB", chunk)
        elif chunk_type == b"IDAT":
            compressed += chunk
        elif chunk_type == b"IEND":
            break
    if width is None or height is None or bit_depth != 8 or color_type not in (2, 6):
        raise ValueError(f"Unsupported PNG: bit_depth={bit_depth}, color_type={color_type}")

    bytes_per_pixel = 3 if color_type == 2 else 4
    raw = zlib.decompress(compressed)
    stride = width * bytes_per_pixel
    rows: list[list[int]] = []
    previous = [0] * stride
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        scanline = raw[offset : offset + stride]
        offset += stride
        row = [0] * stride
        for i, value in enumerate(scanline):
            left = row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
            up = previous[i]
            upper_left = previous[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                predictor_raw = left + up - upper_left
                dist_left = abs(predictor_raw - left)
                dist_up = abs(predictor_raw - up)
                dist_upper_left = abs(predictor_raw - upper_left)
                if dist_left <= dist_up and dist_left <= dist_upper_left:
                    predictor = left
                elif dist_up <= dist_upper_left:
                    predictor = up
                else:
                    predictor = upper_left
            else:
                raise ValueError(f"Unsupported PNG filter {filter_type}")
            row[i] = (value + predictor) & 0xFF
        rows.append(row)
        previous = row
    return width, height, bytes_per_pixel, rows


# detail trimming — we save scores not issues per user instruction


def _strip_issues(p: PenaltyResponse) -> dict:
    return {"penalty_1": p.penalty_1, "penalty_2": p.penalty_2}


# Stage 1


def _s1_messages(prompt_url: str, left_url: str, right_url: str, angle_desc: str) -> list[dict]:
    return [
        {"role": "system", "content": S1_S2BV_S2AC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Reference image (target object):"},
                {"type": "image_url", "image_url": {"url": prompt_url}},
                {"type": "text", "text": "3D model 1:"},
                {"type": "image_url", "image_url": {"url": left_url}},
                {"type": "text", "text": "3D model 2:"},
                {"type": "image_url", "image_url": {"url": right_url}},
                {"type": "text", "text": S1_PROMPT_MATCH_USER.format(angle_desc=angle_desc)},
            ],
        },
    ]


async def _s1_run(
    vlm: AsyncOpenAI,
    sem: asyncio.Semaphore,
    prompt_url: str,
    left_views: str,
    right_views: str,
    seed: int,
) -> dict:
    async def _ask(left_url: str, right_url: str, angle_desc: str) -> PenaltyResponse:
        async with sem:
            r = await _safe_chat_json(
                vlm,
                _s1_messages(prompt_url, left_url, right_url, angle_desc),
                PenaltyResponse,
                label="s1_prompt_match",
                seed=seed,
                temperature=0.0,
                max_tokens=1024,
                max_retries=5,
                on_failure=_neutral_penalty(),
            )
            return cast(PenaltyResponse, r)

    tasks: list[asyncio.Task] = []
    for _theta, _phi, _w, label in S1_ANGLES:
        a_url = _white_url(left_views, label)
        b_url = _white_url(right_views, label)
        desc = S1_ANGLE_DESC[label]
        tasks.append(asyncio.create_task(_ask(a_url, b_url, desc)))
        tasks.append(asyncio.create_task(_ask(b_url, a_url, desc)))
    raw = await asyncio.gather(*tasks)

    angles: list[dict] = []
    n_json_failed = 0
    for i, (theta, phi, weight, label) in enumerate(S1_ANGLES):
        ab = raw[2 * i]
        ba = raw[2 * i + 1]
        pen_a_ab, pen_b_ab = ab.penalty_1, ab.penalty_2
        pen_a_ba, pen_b_ba = ba.penalty_2, ba.penalty_1
        diff_ab = pen_a_ab - pen_b_ab
        diff_ba = pen_a_ba - pen_b_ba
        json_failed = ab.issues == JSON_FAILED_MARKER or ba.issues == JSON_FAILED_MARKER
        if json_failed:
            n_json_failed += 1
        contradictory = json_failed or ((diff_ab > 0 and diff_ba < 0) or (diff_ab < 0 and diff_ba > 0))
        angles.append(
            {
                "theta": theta,
                "phi": phi,
                "weight": weight,
                "label": label,
                "ab": _strip_issues(ab),
                "ba": _strip_issues(ba),
                "pen_a": (pen_a_ab + pen_a_ba) / 2,
                "pen_b": (pen_b_ab + pen_b_ba) / 2,
                "diff_ab": diff_ab,
                "diff_ba": diff_ba,
                "contradictory": contradictory,
                "json_failed": json_failed,
            }
        )
    return {
        "angles": angles,
        "n_total": len(angles),
        "n_contradictory": sum(1 for a in angles if a["contradictory"]),
        "n_consistent": sum(1 for a in angles if not a["contradictory"]),
        "n_json_failed": n_json_failed,
    }


def _s1_direction_consistency(angles: list[dict]) -> tuple[float, float, float, int]:
    w_a = 0.0
    w_b = 0.0
    n_signed = 0
    for a in angles:
        if a["pen_a"] == a["pen_b"]:
            continue
        n_signed += 1
        if a["pen_a"] > a["pen_b"]:
            w_b += a["weight"]
        else:
            w_a += a["weight"]
    total = w_a + w_b
    if total == 0:
        return 1.0, 0.0, 0.0, 0
    return abs(w_a - w_b) / total, w_a, w_b, n_signed


def _s1_aggregate(detail: dict) -> tuple[str, str]:
    raw_angles = detail.get("angles", [])
    consistent = [a for a in raw_angles if not a.get("contradictory")]
    if not consistent:
        return "draw", f"all {len(raw_angles)} angles contradictory -> draw"

    total_w = sum(a["weight"] for a in consistent)
    wpa = sum(a["weight"] * a["pen_a"] for a in consistent) / total_w
    wpb = sum(a["weight"] * a["pen_b"] for a in consistent) / total_w
    diff = wpa - wpb
    avg = (wpa + wpb) / 2.0

    if avg >= S1_BOTH_BAD_AVG and abs(diff) <= S1_BOTH_BAD_MAX_DIFF:
        return "draw", f"wpA={wpa:.2f} wpB={wpb:.2f} avg={avg:.2f} both-bad -> draw"
    if abs(diff) <= S1_DRAW_THRESHOLD:
        return "draw", f"wpA={wpa:.2f} wpB={wpb:.2f} |diff|={abs(diff):.2f} <= {S1_DRAW_THRESHOLD} -> draw"

    consistency, w_a, w_b, n_signed = _s1_direction_consistency(consistent)
    if n_signed >= S1_SPLIT_MIN_SIGNED and consistency < S1_SPLIT_CONSISTENCY_THRESH:
        return "draw", f"split wA={w_a:.1f} wB={w_b:.1f} consistency={consistency:.2f} -> draw"

    choice = "A" if diff < 0 else "B"
    return choice, f"wpA={wpa:.2f} wpB={wpb:.2f} diff={diff:+.2f} -> {choice}"


# Stage 2A — DINOv3 best view + 2 VLM


def _pick_best_view(embeds: dict[str, np.ndarray]) -> tuple[str, float] | None:
    """Pick the view_<name> key with highest cosine similarity to the prompt embedding.

    Producer-side embeddings have only 8 white views; we re-use that same view across
    sides since gray and white share view names.
    """
    if "prompt" not in embeds:
        return None
    prompt_vec = embeds["prompt"]
    candidates = {k: v for k, v in embeds.items() if k.startswith("view_")}
    if not candidates:
        return None
    best_name = max(candidates, key=lambda k: float(np.dot(candidates[k], prompt_vec)))
    return best_name[len("view_") :], float(np.dot(candidates[best_name], prompt_vec))


def _s2bv_messages(prompt_url: str, left_url: str, right_url: str) -> list[dict]:
    return [
        {"role": "system", "content": S1_S2BV_S2AC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Image prompt to generate 3D model:"},
                {"type": "image_url", "image_url": {"url": prompt_url}},
                {"type": "text", "text": "First 3D model (single best prompt-matching view):"},
                {"type": "image_url", "image_url": {"url": left_url}},
                {"type": "text", "text": "Second 3D model (single best prompt-matching view):"},
                {"type": "image_url", "image_url": {"url": right_url}},
                {"type": "text", "text": S2BV_USER_PROMPT},
            ],
        },
    ]


async def _s2bv_run(
    vlm: AsyncOpenAI,
    prompt_url: str,
    left_views: str,
    right_views: str,
    left_embeds: dict[str, np.ndarray] | None,
    right_embeds: dict[str, np.ndarray] | None,
    seed: int,
) -> dict | None:
    if left_embeds is None or right_embeds is None:
        return None
    pick_a = _pick_best_view(left_embeds)
    pick_b = _pick_best_view(right_embeds)
    if pick_a is None or pick_b is None:
        return None
    name_a, sim_a = pick_a
    name_b, sim_b = pick_b
    a_url = _white_url(left_views, name_a)
    b_url = _white_url(right_views, name_b)

    ab, ba = await asyncio.gather(
        _safe_chat_json(
            vlm,
            _s2bv_messages(prompt_url, a_url, b_url),
            PenaltyResponse,
            label="s2bv_ab",
            seed=seed,
            max_retries=5,
            max_tokens=1024,
            on_failure=_neutral_penalty(),
        ),
        _safe_chat_json(
            vlm,
            _s2bv_messages(prompt_url, b_url, a_url),
            PenaltyResponse,
            label="s2bv_ba",
            seed=seed,
            max_retries=5,
            max_tokens=1024,
            on_failure=_neutral_penalty(),
        ),
    )
    ab = cast(PenaltyResponse, ab)
    ba = cast(PenaltyResponse, ba)
    penalty_a = (ab.penalty_1 + ba.penalty_2) / 2
    penalty_b = (ab.penalty_2 + ba.penalty_1) / 2
    return {
        "best_a": {"name": name_a, "similarity": sim_a},
        "best_b": {"name": name_b, "similarity": sim_b},
        "ab": _strip_issues(ab),
        "ba": _strip_issues(ba),
        "penalty_a": penalty_a,
        "penalty_b": penalty_b,
    }


# Stage 2B — artifact_compare


def _s2ac_messages(prompt_url: str, left_url: str, right_url: str) -> list[dict]:
    return [
        {"role": "system", "content": S1_S2BV_S2AC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Image prompt to generate 3D model:"},
                {"type": "image_url", "image_url": {"url": prompt_url}},
                {"type": "text", "text": "First 3D model (4 different views):"},
                {"type": "image_url", "image_url": {"url": left_url}},
                {"type": "text", "text": "Second 3D model (4 different views):"},
                {"type": "image_url", "image_url": {"url": right_url}},
                {"type": "text", "text": S2AC_USER_PROMPT},
            ],
        },
    ]


async def _s2ac_run(
    vlm: AsyncOpenAI,
    prompt_url: str,
    left_views: str,
    right_views: str,
    seed: int,
) -> dict:
    a_url = _grid_url(left_views)
    b_url = _grid_url(right_views)
    ab, ba = await asyncio.gather(
        _safe_chat_json(
            vlm,
            _s2ac_messages(prompt_url, a_url, b_url),
            PenaltyResponse,
            label="s2ac_ab",
            seed=seed,
            max_retries=5,
            max_tokens=1024,
            on_failure=_neutral_penalty(),
        ),
        _safe_chat_json(
            vlm,
            _s2ac_messages(prompt_url, b_url, a_url),
            PenaltyResponse,
            label="s2ac_ba",
            seed=seed,
            max_retries=5,
            max_tokens=1024,
            on_failure=_neutral_penalty(),
        ),
    )
    ab = cast(PenaltyResponse, ab)
    ba = cast(PenaltyResponse, ba)
    penalty_a = (ab.penalty_1 + ba.penalty_2) / 2
    penalty_b = (ab.penalty_2 + ba.penalty_1) / 2
    return {
        "ab": _strip_issues(ab),
        "ba": _strip_issues(ba),
        "penalty_a": penalty_a,
        "penalty_b": penalty_b,
    }


# Stage 2C — checklist_verify


async def _s2cl_decompose(vlm: AsyncOpenAI, prompt_url: str, seed: int) -> PromptChecklist:
    messages: list[dict] = [
        {"role": "system", "content": S2CL_DECOMPOSE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Reference image:"},
                {"type": "image_url", "image_url": {"url": prompt_url}},
                {"type": "text", "text": S2CL_DECOMPOSE_USER_PROMPT},
            ],
        },
    ]
    r = await _safe_chat_json(
        vlm,
        messages,
        PromptChecklist,
        label="s2cl_decompose",
        seed=seed,
        max_retries=5,
        max_tokens=400,
    )
    return cast(PromptChecklist, r)


async def _s2cl_verify(
    vlm: AsyncOpenAI,
    prompt_url: str,
    grid_url: str,
    checklist: PromptChecklist,
    seed: int,
) -> VerificationResult:
    user_text = _s2cl_build_verify_user_prompt(checklist)
    messages: list[dict] = [
        {"role": "system", "content": S2CL_VERIFY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Reference image:"},
                {"type": "image_url", "image_url": {"url": prompt_url}},
                {"type": "text", "text": "3D model (4 views, 2x2 grid):"},
                {"type": "image_url", "image_url": {"url": grid_url}},
                {"type": "text", "text": user_text},
            ],
        },
    ]
    r = await _safe_chat_json(
        vlm,
        messages,
        VerificationResult,
        label="s2cl_verify",
        seed=seed,
        max_retries=5,
        max_tokens=600,
    )
    return cast(VerificationResult, r)


def _s2cl_score(result: VerificationResult) -> int:
    base = sum(S2_CHECKLIST_MATCH_SCORES.get(c.match, 0) for c in result.checks)
    return base + (S2_CHECKLIST_MAIN_BONUS if result.main_object_correct else 0)


def _strip_verify(v: VerificationResult) -> dict:
    return {
        "main_object_correct": v.main_object_correct,
        "checks": [{"feature": c.feature, "match": c.match} for c in v.checks],
    }


async def _s2cl_run(
    vlm: AsyncOpenAI,
    prompt_url: str,
    left_views: str,
    right_views: str,
    seed: int,
) -> dict:
    grid_a = _grid_url(left_views)
    grid_b = _grid_url(right_views)

    try:
        checklist = await _s2cl_decompose(vlm, prompt_url, seed)
    except Exception as exc:
        logger.error(f"s2cl decompose failed: {exc!r}")
        return {"decompose_failed": True, "score_a": 0, "score_b": 0, "gap": 0, "choice": "draw"}

    try:
        verify_a, verify_b = await asyncio.gather(
            _s2cl_verify(vlm, prompt_url, grid_a, checklist, seed + 100),
            _s2cl_verify(vlm, prompt_url, grid_b, checklist, seed + 200),
        )
    except Exception as exc:
        logger.error(f"s2cl verify failed: {exc!r}")
        return {
            "checklist": {
                "main_object": checklist.main_object,
                "features": [f.description for f in checklist.features],
            },
            "verify_failed": True,
            "score_a": 0,
            "score_b": 0,
            "gap": 0,
            "choice": "draw",
        }

    score_a = _s2cl_score(verify_a)
    score_b = _s2cl_score(verify_b)
    gap = score_a - score_b
    if abs(gap) >= S2_CHECKLIST_GAP_THRESHOLD:
        choice = "A" if gap > 0 else "B"
    else:
        choice = "draw"
    return {
        "checklist": {"main_object": checklist.main_object, "features": [f.description for f in checklist.features]},
        "verify_a": _strip_verify(verify_a),
        "verify_b": _strip_verify(verify_b),
        "score_a": score_a,
        "score_b": score_b,
        "gap": gap,
        "choice": choice,
    }


# Stage 2 aggregation


def _s2_penalty_vote(raw: dict | None, min_gap: float) -> tuple[str | None, float | None]:
    if raw is None:
        return None, None
    diff = float(raw["penalty_a"]) - float(raw["penalty_b"])
    if abs(diff) < min_gap:
        return None, diff
    return ("A" if diff < 0 else "B"), diff


def _s2_checklist_vote(detail: dict | None, min_gap: int) -> tuple[str | None, int | None]:
    if detail is None:
        return None, None
    gap = int(detail.get("gap", 0))
    if abs(gap) < min_gap:
        return None, gap
    return ("A" if gap > 0 else "B"), gap


def _s2_strong_voter_choice(
    bv_diff: float | None,
    cl_gap: int | None,
) -> tuple[str | None, str | None]:
    candidates: list[tuple[str, str]] = []
    if bv_diff is not None and abs(bv_diff) >= S2_STRONG_BV_GAP:
        candidates.append(("A" if bv_diff < 0 else "B", f"bv|{bv_diff:+.1f}|"))
    if cl_gap is not None and abs(cl_gap) >= S2_STRONG_CL_GAP:
        candidates.append(("A" if cl_gap > 0 else "B", f"cl|{cl_gap:+d}|"))
    if not candidates:
        return None, None
    sides = {c[0] for c in candidates}
    if len(sides) > 1:
        return None, None
    return candidates[0][0], "+".join(c[1] for c in candidates)


def _s2_consensus(votes: list[str | None], min_agree: int) -> tuple[str, dict[str, int]]:
    counts: Counter = Counter(v for v in votes if v)
    if not counts:
        return "draw", dict(counts)
    top, count = counts.most_common(1)[0]
    if count < min_agree:
        return "draw", dict(counts)
    if count > sum(n for k, n in counts.items() if k != top):
        return top, dict(counts)
    return "draw", dict(counts)


# Stage 3 — gray-bg foreground rescue


def _foreground_stats(rgb_pngs: list[bytes]) -> dict:
    foreground = 0
    pale = 0
    grayish = 0
    luma_sum = 0.0
    for data in rgb_pngs:
        width, height, bpp, rows = _read_png_rgb(data)
        for y in range(0, height, S3_SAMPLE_STEP):
            row = rows[y]
            for x in range(0, width, S3_SAMPLE_STEP):
                i = x * bpp
                red, green, blue = row[i], row[i + 1], row[i + 2]
                bg_distance = max(
                    abs(red - S3_BACKGROUND_RGB[0]),
                    abs(green - S3_BACKGROUND_RGB[1]),
                    abs(blue - S3_BACKGROUND_RGB[2]),
                )
                if bg_distance <= S3_BACKGROUND_TOLERANCE:
                    continue
                foreground += 1
                max_c = max(red, green, blue)
                min_c = min(red, green, blue)
                saturation = (max_c - min_c) / max_c if max_c else 0.0
                luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
                luma_sum += luma
                if saturation < S3_GRAYISH_SAT_MAX:
                    grayish += 1
                if luma > S3_PALE_LUMA_MIN and saturation < S3_PALE_SAT_MAX:
                    pale += 1
    if foreground == 0:
        return {"foreground_samples": 0, "pale_fraction": 0.0, "grayish_fraction": 0.0, "mean_luma": 0.0}
    return {
        "foreground_samples": foreground,
        "pale_fraction": pale / foreground,
        "grayish_fraction": grayish / foreground,
        "mean_luma": luma_sum / foreground,
    }


def _white_unsafe(stats: dict) -> bool:
    return bool(stats["pale_fraction"] >= S3_PALE_FRACTION_MIN and stats["grayish_fraction"] >= S3_GRAYISH_FRACTION_MIN)


async def _s3_run(
    vlm: AsyncOpenAI,
    http: httpx.AsyncClient,
    prompt_url: str,
    left_views: str,
    right_views: str,
    seed: int,
) -> dict:
    a_gray_url = _gray_url(left_views, S3_FRONT_LABEL)
    b_gray_url = _gray_url(right_views, S3_FRONT_LABEL)
    a_png, b_png = await asyncio.gather(
        _fetch_bytes(http, a_gray_url, timeout=60),
        _fetch_bytes(http, b_gray_url, timeout=60),
    )
    if a_png is None or b_png is None:
        return {"fired": False, "choice": None, "reason": "gray PNGs unavailable"}

    try:
        stats = _foreground_stats([a_png, b_png])
    except Exception as exc:
        logger.warning(f"s3 foreground stats failed: {exc}")
        return {"fired": False, "choice": None, "reason": "stats parse failed"}

    if not _white_unsafe(stats):
        return {"stats": stats, "fired": False, "choice": None, "reason": "gate did not fire"}

    user_prompt = S1_PROMPT_MATCH_USER.format(angle_desc=S3_FRONT_DESC)

    def _msg(left: str, right: str) -> list[dict]:
        return [
            {"role": "system", "content": S1_S2BV_S2AC_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Reference image (target object):"},
                    {"type": "image_url", "image_url": {"url": prompt_url}},
                    {"type": "text", "text": "3D model 1:"},
                    {"type": "image_url", "image_url": {"url": left}},
                    {"type": "text", "text": "3D model 2:"},
                    {"type": "image_url", "image_url": {"url": right}},
                    {"type": "text", "text": user_prompt},
                ],
            },
        ]

    ab, ba = await asyncio.gather(
        _safe_chat_json(
            vlm,
            _msg(a_gray_url, b_gray_url),
            PenaltyResponse,
            label="s3_gray_ab",
            seed=seed,
            max_retries=S3_VLM_MAX_RETRIES,
            max_tokens=S3_VLM_MAX_TOKENS,
            on_failure=_neutral_penalty(),
        ),
        _safe_chat_json(
            vlm,
            _msg(b_gray_url, a_gray_url),
            PenaltyResponse,
            label="s3_gray_ba",
            seed=seed + 7,
            max_retries=S3_VLM_MAX_RETRIES,
            max_tokens=S3_VLM_MAX_TOKENS,
            on_failure=_neutral_penalty(),
        ),
    )
    ab = cast(PenaltyResponse, ab)
    ba = cast(PenaltyResponse, ba)
    pen_a = (ab.penalty_1 + ba.penalty_2) / 2
    pen_b = (ab.penalty_2 + ba.penalty_1) / 2
    diff = pen_a - pen_b
    diff_ab = ab.penalty_1 - ab.penalty_2
    diff_ba = ba.penalty_2 - ba.penalty_1
    contradictory = (diff_ab > 0 and diff_ba < 0) or (diff_ab < 0 and diff_ba > 0)
    json_failed = ab.issues == JSON_FAILED_MARKER or ba.issues == JSON_FAILED_MARKER

    if json_failed and contradictory:
        choice = None
    elif contradictory:
        choice = None
    elif abs(diff) < S3_GRAY_DIFF_THRESHOLD:
        choice = None
    else:
        choice = "A" if diff < 0 else "B"

    return {
        "stats": stats,
        "fired": True,
        "ab": _strip_issues(ab),
        "ba": _strip_issues(ba),
        "pen_a": pen_a,
        "pen_b": pen_b,
        "diff": diff,
        "contradictory": contradictory,
        "json_failed": json_failed,
        "choice": choice,
    }


# Stage 4 — per-angle side guard


def _neutral_side_guard() -> SideGuardVerdict:
    """Fail-safe on JSON parse failure: don't demote a side on a bad response."""
    return SideGuardVerdict(verdict="ok", reason=JSON_FAILED_MARKER)


async def _s4_ask_per_angle(
    vlm: AsyncOpenAI,
    front_url: str,
    angle_url: str,
    angle_desc: str,
    seed: int,
) -> str:
    """Ask the VLM whether `angle_desc` view is consistent with the front. Returns "ok" / "garbage".

    Uses JSON-schema response_format like every other stage — without it, GLM-style
    thinking models burn the whole `S4_VLM_MAX_TOKENS` budget on `<think>...</think>`
    before reaching the verdict. The schema constrains the model to emit a clean
    `{"verdict": ..., "reason": ...}` and nothing else.
    """
    messages: list[dict] = [
        {"role": "system", "content": S4_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Front view of the 3D model:"},
                {"type": "image_url", "image_url": {"url": front_url}},
                {"type": "text", "text": f"Same 3D model viewed from {angle_desc}:"},
                {"type": "image_url", "image_url": {"url": angle_url}},
                {"type": "text", "text": S4_PER_ANGLE_PROMPT.format(angle_desc=angle_desc)},
            ],
        },
    ]
    parsed = await _safe_chat_json(
        vlm,
        messages,
        SideGuardVerdict,
        label="s4_per_angle",
        seed=seed,
        max_retries=S4_VLM_MAX_RETRIES,
        max_tokens=S4_VLM_MAX_TOKENS,
        on_failure=_neutral_side_guard(),
    )
    return cast(SideGuardVerdict, parsed).verdict


def _s4_aggregate_side(verdicts: list[str], k_threshold: int) -> str:
    n_garbage = sum(1 for v in verdicts if v == "garbage")
    return "garbage" if n_garbage >= k_threshold else "ok"


def _s4_step_down(primary: str, side_a: str, side_b: str) -> tuple[str, str]:
    a_bad = side_a == "garbage"
    b_bad = side_b == "garbage"
    if a_bad == b_bad:
        return primary, "primary"
    if a_bad:
        if primary == "A":
            return "draw", "step_down"
        if primary == "draw":
            return "B", "step_down"
        return primary, "primary"
    if primary == "B":
        return "draw", "step_down"
    if primary == "draw":
        return "A", "step_down"
    return primary, "primary"


async def _s4_run(
    vlm: AsyncOpenAI,
    left_views: str,
    right_views: str,
    primary: str,
    seed: int,
) -> dict:
    async def _angle_verdicts(views_prefix: str, seed_off: int) -> list[dict]:
        front_url = _white_url(views_prefix, S4_FRONT_LABEL)

        async def _one(label: str, angle_desc: str, off: int) -> dict:
            verdict = await _s4_ask_per_angle(
                vlm,
                front_url,
                _white_url(views_prefix, label),
                angle_desc,
                seed + seed_off + off,
            )
            return {"label": label, "verdict": verdict}

        return await asyncio.gather(
            *[_one(label, desc, off * 100) for off, (label, desc) in enumerate(S4_SIDE_ANGLES)]
        )  # type: ignore[return-value]

    angles_a, angles_b = await asyncio.gather(
        _angle_verdicts(left_views, 5000),
        _angle_verdicts(right_views, 6000),
    )
    side_a = _s4_aggregate_side([r["verdict"] for r in angles_a], S4_K_THRESHOLD)
    side_b = _s4_aggregate_side([r["verdict"] for r in angles_b], S4_K_THRESHOLD)
    choice, source = _s4_step_down(primary, side_a, side_b)

    return {
        "k_threshold": S4_K_THRESHOLD,
        "primary": primary,
        "side_verdicts": {"a": side_a, "b": side_b},
        "n_garbage": {
            "a": sum(1 for r in angles_a if r["verdict"] == "garbage"),
            "b": sum(1 for r in angles_b if r["verdict"] == "garbage"),
        },
        "per_angle": {"a": angles_a, "b": angles_b},
        "decision_source": source,
        "applied": choice != primary,
        "choice": choice,
    }


# Final summary call — one extra VLM round-trip per duel that produces a
# human-readable comparison of the two models' issues. Runs in parallel with the
# stage pipeline so it doesn't add to wall-clock latency in the common path.


async def _explain_run(
    vlm: AsyncOpenAI,
    prompt_url: str,
    left_grid_url: str,
    right_grid_url: str,
    seed: int,
) -> str:
    """One VLM call returning a short text comparing the two models' visible issues.

    Failure-tolerant: a parse failure returns "" so the duel itself is unaffected.
    """
    messages: list[dict] = [
        {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Reference image:"},
                {"type": "image_url", "image_url": {"url": prompt_url}},
                {"type": "text", "text": "Model A (4 views):"},
                {"type": "image_url", "image_url": {"url": left_grid_url}},
                {"type": "text", "text": "Model B (4 views):"},
                {"type": "image_url", "image_url": {"url": right_grid_url}},
                {"type": "text", "text": EXPLAIN_USER_PROMPT},
            ],
        },
    ]
    parsed = await _safe_chat_json(
        vlm,
        messages,
        IssuesSummary,
        label="explain",
        seed=seed,
        max_retries=3,
        max_tokens=300,
        on_failure=_neutral_issues(),
    )
    return cast(IssuesSummary, parsed).issues


# Top-level entry point


CHOICE_TO_WINNER: dict[str, DuelWinner] = {
    "A": DuelWinner.LEFT,
    "B": DuelWinner.RIGHT,
    "draw": DuelWinner.DRAW,
}


# Slim per-stage detail builders. Keep miner-actionable signal; drop operator/debug
# fields and anything that lets a miner reverse-engineer the judge.


def _slim_s1(s1: dict, choice: str) -> dict:
    """S1 detail — aggregate + per-angle pen_a/pen_b. Drops AB/BA splits and internal flags."""
    return {
        "choice": choice,
        "n_total": s1["n_total"],
        "n_consistent": s1["n_consistent"],
        "angles": [{"label": a["label"], "pen_a": a["pen_a"], "pen_b": a["pen_b"]} for a in s1["angles"]],
    }


def _slim_penalty_pair(d: dict | None) -> dict | None:
    """For best_view / artifact_compare. Keeps penalties; drops AB/BA and best-view names."""
    if d is None:
        return None
    return {"penalty_a": d["penalty_a"], "penalty_b": d["penalty_b"]}


def _slim_checklist(cl: dict | None) -> dict | None:
    """Checklist — the miner's most actionable feedback (which features matched/missed).

    `_s2cl_run` already stores `features` as a list of description strings (not the
    full `ChecklistItem` dicts), so we index by string here.
    """
    if cl is None or "checklist" not in cl:
        return cl  # decompose/verify failed; keep the failure marker as-is
    inner = cl["checklist"]
    verify_a = cl.get("verify_a", {})
    verify_b = cl.get("verify_b", {})
    feature_descs: list[str] = list(inner.get("features", []))
    checks_a = {c["feature"]: c["match"] for c in verify_a.get("checks", [])}
    checks_b = {c["feature"]: c["match"] for c in verify_b.get("checks", [])}
    return {
        "main_object": inner["main_object"],
        "main_object_correct_a": verify_a.get("main_object_correct"),
        "main_object_correct_b": verify_b.get("main_object_correct"),
        "features": [{"feature": d, "side_a": checks_a.get(d), "side_b": checks_b.get(d)} for d in feature_descs],
        "score_a": cl.get("score_a"),
        "score_b": cl.get("score_b"),
    }


def _slim_s2(
    bv: dict | None,
    ac: dict,
    cl: dict,
    choice: str,
    source: str,
) -> dict:
    """S2 detail — sub-judge penalties + checklist features with per-side matches."""
    return {
        "choice": choice,
        "source": source,
        "best_view": _slim_penalty_pair(bv),
        "artifact_compare": _slim_penalty_pair(ac),
        "checklist": _slim_checklist(cl),
    }


def _slim_s3(s3: dict) -> dict:
    """S3 detail — just whether the gate fired and what it decided. Pixel stats dropped."""
    return {"fired": s3.get("fired", False), "choice": s3.get("choice")}


def _slim_s4(s4: dict) -> dict:
    """S4 detail — per-angle ok/garbage verdicts per side + final choice. Mechanics dropped."""
    return {
        "choice": s4["choice"],
        "side_verdicts": s4["side_verdicts"],
        "per_angle": {side: {a["label"]: a["verdict"] for a in s4["per_angle"][side]} for side in ("a", "b")},
    }


async def evaluate_duel(
    vlm: AsyncOpenAI,
    http: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    prompt_url: str,
    left_gen: GenerationResult,
    right_gen: GenerationResult,
    seed: int,
    log_id: str = "",
) -> tuple[DuelWinner, dict]:
    """Run the full multi-stage pipeline. Returns (winner, detail).

    `detail` carries per-stage scores (no `issues` text, no full responses) and is
    intended to be persisted on `DuelReport.detail`. Missing-preview short-circuits
    return early with a draw/win-by-default and a slim detail payload.

    `log_id` is a free-form context tag (typically `"<stem> / <left> vs <right>"`) used
    only for log lines. Per-stage entry/exit is logged at DEBUG with stage-local elapsed;
    the final outcome is logged at INFO with the deciding stage and duel wall-clock from
    the moment the first stage starts work (excluding asyncio scheduling delay).

    The persisted detail is intentionally trimmed to miner-actionable signal only —
    aggregate/per-angle penalties, sub-judge scores, the prompt-checklist with per-side
    matches, and S4 per-angle verdicts. Operator-only mechanics (DINOv3 best-view
    similarity, S3 pixel stats, raw AB/BA splits, internal flags like `applied`,
    `decision_source`, `contradictory`) are dropped to avoid both noise and gameability.
    """
    detail: dict[str, Any] = {}
    duel_start = asyncio.get_running_loop().time()

    # Preview-existence gate.
    left_views = left_gen.views
    right_views = right_gen.views
    if not left_views and not right_views:
        logger.debug(f"{log_id}: winner=DRAW (no previews on either side)")
        return DuelWinner.DRAW, {**detail, "reason": "no previews on either side"}
    if not left_views:
        logger.debug(f"{log_id}: winner=RIGHT (no preview from left)")
        return DuelWinner.RIGHT, {**detail, "reason": "no preview from left"}
    if not right_views:
        logger.debug(f"{log_id}: winner=LEFT (no preview from right)")
        return DuelWinner.LEFT, {**detail, "reason": "no preview from right"}

    # Kick off the explain call up front — runs concurrent with the whole pipeline so
    # its latency is hidden under whatever stage decides. Awaited at every return path.
    explain_task = asyncio.create_task(
        _explain_run(vlm, prompt_url, f"{left_views}/grid.png", f"{right_views}/grid.png", seed)
    )

    # Stage 1: split-aware front prompt-match
    t = asyncio.get_running_loop().time()
    s1 = await _s1_run(vlm, sem, prompt_url, left_views, right_views, seed)
    s1_choice, _s1_reason = _s1_aggregate(s1)
    detail["s1"] = _slim_s1(s1, s1_choice)
    logger.debug(f"{log_id}: S1 -> {s1_choice} ({asyncio.get_running_loop().time() - t:.1f}s)")
    if s1_choice != "draw":
        detail["decided_by"] = "S1"
        detail["issues"] = await explain_task
        logger.info(
            f"{log_id}: winner={CHOICE_TO_WINNER[s1_choice].value} (decided by S1, "
            f"duel {asyncio.get_running_loop().time() - duel_start:.1f}s)"
        )
        return CHOICE_TO_WINNER[s1_choice], detail

    # Stage 2: all three sub-judges in parallel. best_view loads its own embeddings
    # internally (the only thing it depends on), so it doesn't have to wait on ac/cl.
    async def _bv_with_embeddings() -> dict | None:
        left_embeds, right_embeds = await asyncio.gather(
            _load_embeddings(http, left_views),
            _load_embeddings(http, right_views),
        )
        return await _s2bv_run(vlm, prompt_url, left_views, right_views, left_embeds, right_embeds, seed)

    t = asyncio.get_running_loop().time()
    bv, ac, cl = await asyncio.gather(
        _bv_with_embeddings(),
        _s2ac_run(vlm, prompt_url, left_views, right_views, seed),
        _s2cl_run(vlm, prompt_url, left_views, right_views, seed),
    )

    bv_vote, bv_diff = _s2_penalty_vote(bv, S2_BV_MIN_GAP)
    ac_vote, ac_diff = _s2_penalty_vote(ac, S2_AC_MIN_GAP)
    cl_vote, cl_gap = _s2_checklist_vote(cl, S2_CL_MIN_GAP)

    strong_choice, strong_tag = _s2_strong_voter_choice(bv_diff, cl_gap)
    s2_decided_by: str
    if strong_choice is not None:
        detail["s2"] = _slim_s2(bv, ac, cl, choice=strong_choice, source="strong_voter")
        primary = strong_choice
        s2_decided_by = f"S2 strong_voter [{strong_tag}]"
        logger.debug(
            f"{log_id}: S2 -> {strong_choice} via strong_voter [{strong_tag}] "
            f"({asyncio.get_running_loop().time() - t:.1f}s)"
        )
    else:
        consensus_choice, _counts = _s2_consensus([bv_vote, ac_vote, cl_vote], S2_MIN_CONSENSUS)
        detail["s2"] = _slim_s2(bv, ac, cl, choice=consensus_choice, source="consensus_3")
        logger.debug(
            f"{log_id}: S2 -> {consensus_choice} via consensus "
            f"[bv={bv_vote},ac={ac_vote},cl={cl_vote}] "
            f"({asyncio.get_running_loop().time() - t:.1f}s)"
        )
        if consensus_choice != "draw":
            primary = consensus_choice
            s2_decided_by = "S2 consensus"
        else:
            # Stage 3: gray rescue
            t3 = asyncio.get_running_loop().time()
            s3 = await _s3_run(vlm, http, prompt_url, left_views, right_views, seed)
            detail["s3"] = _slim_s3(s3)
            s3_choice = s3.get("choice")
            logger.debug(
                f"{log_id}: S3 -> {s3_choice or 'no_change'} "
                f"(fired={s3.get('fired')}, {asyncio.get_running_loop().time() - t3:.1f}s)"
            )
            primary = s3_choice or "draw"
            s2_decided_by = "S3 gray-rescue" if s3_choice else "S3 (gate did not fire) → draw"

    # Stage 4 always runs (side guard with step-down).
    t = asyncio.get_running_loop().time()
    s4 = await _s4_run(vlm, left_views, right_views, primary, seed)
    detail["s4"] = _slim_s4(s4)
    final = s4["choice"]
    s4_applied = s4.get("applied", False)
    logger.debug(
        f"{log_id}: S4 -> {final} (applied={s4_applied}, primary={primary}, "
        f"{asyncio.get_running_loop().time() - t:.1f}s)"
    )

    deciding = f"S4 step-down (was {primary})" if s4_applied else s2_decided_by
    detail["decided_by"] = deciding
    detail["issues"] = await explain_task
    logger.info(
        f"{log_id}: winner={CHOICE_TO_WINNER[final].value} (decided by {deciding}, "
        f"duel {asyncio.get_running_loop().time() - duel_start:.1f}s)"
    )
    return CHOICE_TO_WINNER[final], detail
