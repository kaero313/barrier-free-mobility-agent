from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\((?P<target>[^)]+)\)")
PUBLIC_DOCUMENTS = [PROJECT_ROOT / "README.md", *(PROJECT_ROOT / "docs").rglob("*.md")]


def test_local_documentation_links_resolve() -> None:
    missing: list[str] = []
    for document in PUBLIC_DOCUMENTS:
        content = document.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(content):
            raw_target = match.group("target").strip().strip("<>")
            target = raw_target.split("#", maxsplit=1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{document.relative_to(PROJECT_ROOT)} -> {raw_target}")

    assert missing == []


def test_readme_opens_with_the_product_problem_and_runtime_boundary() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.startswith(
        "# Barrier-Free Mobility Agent\n\n"
        "교통약자의 자연어 이동 질문을 이해하고 Barrier-Free Mobility MCP의 도구를 "
        "안전하게 조합하는 웹 기반 AI Agent 제품입니다."
    )
    assert "## 책임 경계" in readme
    assert "## 현재 구현" in readme
    assert "## 범위에서 제외하는 것" in readme


def test_product_and_operations_surfaces_have_separate_contracts() -> None:
    information_architecture = (PROJECT_ROOT / "docs" / "information-architecture.md").read_text(
        encoding="utf-8"
    )
    public_status = (PROJECT_ROOT / "docs" / "public-status.md").read_text(encoding="utf-8")
    decision = (
        PROJECT_ROOT / "docs" / "adr" / "0008-product-trust-and-operations-surfaces.md"
    ).read_text(encoding="utf-8")

    assert "Public answer projection" in information_architecture
    assert "Public status projection" in information_architecture
    assert "Internal ops projection" in information_architecture
    assert "same-origin BFF가 별도의 allowlist DTO로 변환한다" in public_status
    assert ("결과가 생기기 전에는\n`판단 대기 중` panel을 렌더링하지 않는다") in decision


def test_product_design_keeps_current_and_target_capabilities_separate() -> None:
    product_design = (PROJECT_ROOT / "docs" / "product-design.md").read_text(encoding="utf-8")

    assert (
        "Phase 2C의 conversational thread API는 이동 경로 접근성 질의를 "
        "기본으로 지원하고" in product_design
    )
    assert "Web은 질문 문구를 해석해 field나 극성을 만들지 않는다" in product_design
    assert "가상의 node 진행률을 만들지 않는다" in product_design
    assert "AgentState" in product_design


def test_context_budget_design_keeps_state_model_and_retention_boundaries_separate() -> None:
    adr = (
        PROJECT_ROOT / "docs" / "adr" / "0006-bounded-model-context-and-token-accounting.md"
    ).read_text(encoding="utf-8")
    architecture = (PROJECT_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    assert "`ConversationDigest`라는 두 번째 영속 모델은\n추가하지 않는다" in adr
    assert "fresh non-pending turn만 ambiguous model admission 대상" in adr
    assert "`AgentState.run_usage`가 admission과 resume budget의 authority" in adr
    assert "`agent_runs.usage`는 마지막 성공 repository transaction의 운영 read-model" in adr
    assert "checkpoint snapshot retention과 token compaction은 별도 작업" in adr
    assert "실제 model 호출은 0회" in architecture


def test_stage_a_completion_and_mcp_bounds_are_documented() -> None:
    implementation = (PROJECT_ROOT / "docs" / "implementation-plan.md").read_text(encoding="utf-8")
    architecture = (PROJECT_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    assert "ADR-0006 Stage A 구현 완료" in implementation
    assert "Stage A — state/accounting 기반선 (완료, 2026-07-12)" in implementation
    assert "MCP HTTP 응답은 압축을 거부하고 일반 body는 1 MiB" in architecture
    assert "SSE는 event당 1 MiB를 넘기기 전에 중단" in architecture
    assert "세션별 value-free violation latch" in architecture
    assert "HTTP client를 먼저 bounded abort" in architecture
    assert "graceful DELETE도 별도 250ms best-effort 예산" in architecture
    assert "Tool discovery도 최대 16 page·64 Tool·1 MiB로 제한" in architecture
