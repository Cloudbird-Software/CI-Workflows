#!/usr/bin/env bash
# flaky-retry.sh —— 测试重试包装器（P2-9，ADR-0043）：失败自动重试 ≤ retry_max，
# 每次尝试入账（FLAKY-RETRY 行 + step summary）；仅"失败→通过"转移记 flaky 事件
# （FLAKY-EVENT）——确定性失败重试全败仍红，不产生 flaky 记录（真回归不误放）。
# retry_max 真源 = .github 仓 governance/policy/testing.yaml#flaky_governance.retry_max
# （拉取失败 = exit 2 fail-closed，不裸奔内置缺省）。
# self-test: bash flaky-retry.sh --self-test
set -uo pipefail
ORG="${ORG:-Cloudbird-Software}"
POLICY_URL="https://api.github.com/repos/$ORG/.github/contents/governance/policy/testing.yaml"
POLICY_REF="${POLICY_REF:-main}"

if [[ "${1:-}" == "--self-test" ]]; then
  PASS=0; FAIL=0
  t() { # name expected_rc expected_flaky cmd...
    local name="$1" want_rc="$2" want_flaky="$3"; shift 3
    local out rc flaky
    out=$("$@" 2>&1); rc=$?
    flaky=$(grep -c "FLAKY-EVENT" <<<"$out" || true)
    if [[ "$rc" == "$want_rc" && "$flaky" == "$want_flaky" ]]; then
      echo "  PASS $name (rc=$rc flaky=$flaky)"; PASS=$((PASS+1))
    else
      echo "  FAIL $name (rc=$rc want=$want_rc flaky=$flaky want=$want_flaky)"; FAIL=$((FAIL+1))
    fi
  }
  POLICY_REF=x POLICY_URL="file:///dev/null" true 2>/dev/null || :
  # 三类核心用例（#94 T1/T2/T3 等价）：确定性失败/必然通过/奇偶翻转
  export FLAKY_POLICY_INLINE='{"retry_max":2}'
  t "确定性失败(重试2全败→红,无flaky)" 1 0 bash "$0" --always-fail
  t "必然通过(绿,无flaky)" 0 0 bash "$0" --always-pass
  t "奇偶翻转(首败重试过→绿+flaky)" 0 1 bash "$0" --flip
  echo "self-test: PASS=$PASS FAIL=$FAIL"
  [[ $FAIL -eq 0 ]] || exit 1
  exit 0
fi

# policy 拉取（fail-closed）
if [[ -n "${FLAKY_POLICY_INLINE:-}" ]]; then
  RETRY_MAX=$(grep -oE '"'retry_max'"?[[:space:]]*:[[:space:]]*[0-9]+' <<<"$FLAKY_POLICY_INLINE" | grep -oE '[0-9]+')
else
  P=$(curl -sS -H "Accept: application/vnd.github+json" "$POLICY_URL?ref=$POLICY_REF" \
      | jq -r '.content' | base64 -d 2>/dev/null | sed -n '/^flaky_governance:/,/^[a-z_]*:$/p' \
      | grep -E '^\s+retry_max:' | head -1 | grep -oE '[0-9]+')
  if [[ -z "$P" ]]; then
    echo "::error::flaky_governance policy 拉取/解析失败（$POLICY_URL@$POLICY_REF）——fail-closed，不裸奔缺省（ADR-0043）"
    exit 2
  fi
  RETRY_MAX=$P
fi
TOTAL=$(( RETRY_MAX + 1 ))

CMD=("$@")
if [[ "${1:-}" == "--always-fail" ]]; then CMD=(bash -c 'exit 1');
elif [[ "${1:-}" == "--always-pass" ]]; then CMD=(bash -c 'exit 0');
elif [[ "${1:-}" == "--flip" ]]; then CMD=(bash -c 'f=/tmp/flaky-flip-state; if [ -f "$f" ]; then rm -f "$f"; exit 0; else touch "$f"; exit 1; fi'); fi

ATTEMPT=0
while :; do
  ATTEMPT=$((ATTEMPT+1))
  echo "::group::FLAKY-RETRY attempt $ATTEMPT/$TOTAL"
  "${CMD[@]}"
  RC=$?
  echo "::endgroup::"
  if [[ $RC -eq 0 ]]; then
    echo "FLAKY-STATS attempt=$ATTEMPT/$TOTAL outcome=pass"
    { echo ""; echo "## FLAKY-RETRY"; echo "- attempts: $ATTEMPT/$TOTAL"; } >> "$${GITHUB_STEP_SUMMARY:-/dev/null}"
    if [[ $ATTEMPT -gt 1 ]]; then
      echo "FLAKY-EVENT test=suite-level granularity=coarse note='失败→重试通过转移（ADR-0043：记 1 次 flaky 事件；逐测试粒度待 junit 解析）'"
    fi
    exit 0
  fi
  if [[ $ATTEMPT -ge $TOTAL ]]; then
    echo "FLAKY-STATS attempt=$ATTEMPT/$TOTAL outcome=fail retries_exhausted=true"
    echo "::error::测试确定性失败（重试 $RETRY_MAX 次全败）——非 flaky，不产生 flaky 记录"
    exit 1
  fi
  echo "FLAKY-RETRY attempt=$ATTEMPT failed，重试（$((TOTAL-ATTEMPT)) 次剩余）"
done
