import argparse
import asyncio
import csv
import random
from pathlib import Path
from pprint import pprint

import httpx
import numpy as np
import pybase64
import torch
from openai import AsyncOpenAI
from pydantic import BaseModel

SYSTEM_PROMPT = "You are a specialized 3D model evaluation system. Analyze visual quality and prompt adherence with expert precision. Always respond with valid JSON only."
USER_PROMPT_IMAGE = """Does each 3D model match the image prompt?

Penalty 0-10:
0 = Perfect match
3 = Minor issues (slight shape differences, missing small details)
5 = Moderate issues (wrong style, significant details missing)
7 = Major issues (wrong category but related, e.g. chair vs stool)
10 = Completely wrong object

Output: {"penalty_1": <0-10>, "penalty_2": <0-10>, "issues": "<brief>"}"""

DEFAULT_PROMPT_EXTS = (".png", ".jpg", ".jpeg")
DEFAULT_RENDER_EXTS = (".png",)


class JudgeResponse(BaseModel):
    """Response from a judge evaluating a duel between two models."""

    penalty_1: int
    """Penalty for the first model (0-10, lower is better)."""
    penalty_2: int
    """Penalty for the second model (0-10, lower is better)."""
    issues: str
    """Human-readable issue summary produced by the judge."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic duels between two render roots.")
    parser.add_argument("--left-folder", type=str, required=True, help="Folder containing the first set of 2x2 renders.")
    parser.add_argument("--right-folder", type=str, required=True, help="Folder containing the second set of 2x2 renders.")
    parser.add_argument("--prompt-folder", type=str, default=None, help="Optional folder mirroring the render structure with the image prompts.")
    parser.add_argument("--output-folder", type=str, default="./outputs/duel_results", help="Folder where duel results CSV is written.")
    parser.add_argument("--vllm-url", type=str, default="http://localhost:8095/v1", help="Base URL for the vLLM server.")
    parser.add_argument("--sampling-seed", type=int, default=404, help="Seed applied to vLLM sampling parameters to make duels deterministic.")
    return parser.parse_args()


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def collect_images(root: Path, extensions: tuple[str, ...]) -> dict[str, Path]:
    if not root.exists():
        raise FileNotFoundError(f"Missing directory: {root}")

    ext_order = {ext.lower(): idx for idx, ext in enumerate(extensions)}
    images: dict[str, Path] = {}

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in ext_order:
            continue
        key = str(path.relative_to(root).with_suffix("").as_posix())
        if key in images:
            continue
        images[key] = path

    return images


async def load_b64_from_file(path: Path | str) -> str:
    with open(path, "rb") as f:
        return pybase64.b64encode(f.read()).decode()


async def judge_duel(
    prompt_b64: str,
    render1: str,
    render2: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
    sampling_params: dict | None = None,
) -> dict:
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=20.0,
        http_client=httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
        ),
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Image prompt to generate 3D model:"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{prompt_b64}"},
                },
                {"type": "text", "text": "First 3D model (4 different views):"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{render1}"},
                },
                {"type": "text", "text": "Second 3D model (4 different views):"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{render2}"},
                },
                {"type": "text", "text": USER_PROMPT_IMAGE},
            ],
        },
    ]

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "judge-response",
            "schema": JudgeResponse.model_json_schema(),
        },
    }

    completion = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        extra_body={"sampling_params": sampling_params} if sampling_params is not None else None,
    )

    return eval(completion.choices[0].message.content)


async def process_one(duel_entry: dict, llm_kwargs: dict) -> tuple:
    duel_id = duel_entry["duel_id"]
    left_path = Path(duel_entry["left_path"])
    right_path = Path(duel_entry["right_path"])
    prompt_path = Path(duel_entry["image_path"])
    render_0001 = await load_b64_from_file(left_path)
    render_0002 = await load_b64_from_file(right_path)
    prompt_b64 = await load_b64_from_file(prompt_path)

    duel_result = await judge_duel(render1=render_0001, render2=render_0002, prompt_b64=prompt_b64, **llm_kwargs)
    duel_result_switch = await judge_duel(render1=render_0002, render2=render_0001, prompt_b64=prompt_b64, **llm_kwargs)

    penalty_0001_direct = duel_result["penalty_1"]
    penalty_0002_direct = duel_result["penalty_2"]
    penalty_0002_switch = duel_result_switch["penalty_1"]
    penalty_0001_switch = duel_result_switch["penalty_2"]

    avg_penalty_0001 = (penalty_0001_direct + penalty_0001_switch) / 2
    avg_penalty_0002 = (penalty_0002_direct + penalty_0002_switch) / 2

    penalty_diff = abs(avg_penalty_0001 - avg_penalty_0002)

    left_issues = duel_result.get("issues", "N/A")
    right_issues = duel_result_switch.get("issues", "N/A")
    combined_issues = f"Direct pass: {left_issues}. Swapped pass: {right_issues}"

    if penalty_diff <= 1:
        winner = "draw"
    elif avg_penalty_0001 < avg_penalty_0002:
        winner = "left"
    else:
        winner = "right"
    
    pprint(f"Prompt: {prompt_path.name}")
    pprint(f"Winner: {winner}")
    pprint(f"Direct pass: {left_issues}")
    pprint(f"Swapped pass: {right_issues}")
    
    return (
        duel_id,
        winner,
        left_issues,
        right_issues,
        prompt_path,
        left_path,
        right_path,
    )


def save_duel_results_csv(
    duel_data: list,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "duel_id",
            "winner",
            "image_path",
            "left_path",
            "right_path",
            "left_issues",
            "right_issues",
        ])
        for duel_id, winner, left_issues, right_issues, image_path, left_path, right_path in duel_data:
            writer.writerow([
                duel_id,
                winner,
                str(image_path),
                str(left_path),
                str(right_path),
                left_issues,
                right_issues,
            ])


async def main():
    args = parse_args()
    seed_everything(args.sampling_seed)

    left_root = Path(args.left_folder)
    right_root = Path(args.right_folder)
    prompt_root = Path(args.prompt_folder) if args.prompt_folder else None

    left_images = collect_images(left_root, DEFAULT_RENDER_EXTS)
    right_images = collect_images(right_root, DEFAULT_RENDER_EXTS)
    prompt_images = collect_images(prompt_root, DEFAULT_PROMPT_EXTS) if prompt_root else {}

    if not left_images or not right_images:
        raise SystemExit("Left or right folder does not contain render images.")

    common_keys = sorted(set(left_images) & set(right_images))
    if not common_keys:
        raise SystemExit("No matching render filenames between left and right folders.")

    duels = []
    for idx, rel_path in enumerate(common_keys, start=1):
        prompt_candidate = prompt_images.get(rel_path)
        if prompt_candidate is None:
            continue
        duels.append(
            {
                "duel_id": idx,
                "image_path": str(prompt_candidate),
                "left_path": str(left_images[rel_path]),
                "right_path": str(right_images[rel_path]),
            }
        )

    if not duels:
        raise SystemExit("No duels configured (maybe prompts were skipped).")

    left_count = right_count = draw_count = 0
    duel_data = []

    llm_kwargs = {
        "model": "THUDM/GLM-4.1V-9B-Thinking",
        "base_url": args.vllm_url,
        "api_key": "local",
        "temperature": 0.0,
        "max_tokens": 1024,
        "sampling_params": {"seed": args.sampling_seed},
    }

    total_duels = len(duels)
    for idx, duel in enumerate(duels, start=1):
        try:
            print("-" * 20)
            print(f"Duel {idx}/{total_duels}")
            duel_id, winner, left_issues, right_issues, prompt_path, left_path, right_path = await process_one(
                duel, llm_kwargs
            )
        except FileNotFoundError as exc:
            print(f"Skipping duel {duel.get('duel_id')}: {exc}")
            continue

        if winner == "left":
            left_count += 1
        elif winner == "right":
            right_count += 1
        elif winner == "draw":
            draw_count += 1

        duel_data.append((duel_id, winner, left_issues, right_issues, prompt_path, left_path, right_path))

    print("-" * 20)
    print(f"Duel summary:")
    print("-" * 20)
    print(f"Left wins: {left_count}")
    print(f"Right wins: {right_count}")
    print(f"Draws: {draw_count}\n")

    output_folder = Path(args.output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)
    output_csv = output_folder / f"{left_root.name}_vs_{right_root.name}.csv"
    save_duel_results_csv(duel_data, output_csv)
    print(f"Duel results saved to {output_csv}\n")


if __name__ == "__main__":
    asyncio.run(main())
