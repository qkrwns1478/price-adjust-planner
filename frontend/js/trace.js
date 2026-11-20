// 실행 과정 표시 (F3019)
// 실행 이벤트를 발생 순서대로 받아 단계 → Tool 호출 순으로 그리고, 재계획 지점을 구분한다.
// 대화 화면은 스트림으로 받은 이벤트를, 실행 로그 화면은 저장된 이벤트를 같은 방식으로 넘긴다.

import {
  INTENT,
  JUDGMENT,
  NODE,
  STRATEGY,
  TOOL,
  TOOL_ERROR,
  VERDICT,
  WORKER_ACTION,
  argsText,
  ms,
} from "./labels.js";

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function setState(node, state) {
  node.classList.remove(
    "is-running",
    "is-ok",
    "is-fail",
    "is-warn",
    "is-muted",
  );
  node.classList.add(`is-${state}`);
}

export class RunTrace {
  constructor(message) {
    this.root = el("article", "run is-running");
    const head = el("header", "run-head");
    this.msg = el("p", "run-msg", message || "");
    this.meta = el("p", "run-meta", "요청을 보내는 중");
    head.append(this.msg, this.meta);
    this.list = el("ol", "steps");
    this.root.append(head, this.list);
    this.steps = {}; // node → 가장 최근 단계 li
    this.calls = {}; // round:step → call li
    this.lastStep = null;
    this.finished = false;
  }

  step(node) {
    const li = el("li", "step is-running");
    const line = el("div", "step-line");
    li._info = el("span", "step-info");
    li._time = el("span", "step-time");
    line.append(
      el("span", "mark"),
      el("span", "step-name", NODE[node] || node),
      li._info,
      li._time,
    );
    li.append(line);
    this.list.append(li);
    this.steps[node] = li;
    this.lastStep = li;
    return li;
  }

  note(text, state = "muted", target = this.lastStep) {
    const p = el("p", `note is-${state}`, text);
    (target || this.root).append(p);
  }

  call(d, state, extra) {
    const host = this.steps.execute || this.lastStep || this.step("execute");
    if (!host._calls) {
      host._calls = el("ol", "calls");
      host.append(host._calls);
    }
    const li = el("li", `call is-${state}`);
    li._time = el("span", "call-time");
    li.append(
      el("span", "mark"),
      el("span", "call-name", TOOL[d.tool] || d.tool),
      el("span", "call-args", argsText(d.args)),
    );
    if (d.attempt) li.append(el("span", "tag", `재시도 ${d.attempt}`));
    if (extra) li.append(el("span", "tag", extra));
    li.append(li._time);
    host._calls.append(li);
    this.calls[this.key(d)] = li;
    this.lastCall = li;
    return li;
  }

  key(d) {
    return `${d.round}:${d.step}:${d.attempt || 0}`;
  }

  failCall(li, code) {
    setState(li, code === "SKIPPED" ? "muted" : "fail");
    li.append(
      el(
        "p",
        "call-error",
        `${TOOL_ERROR[code] || "호출에 실패했습니다"} (${code})`,
      ),
    );
  }

  add(e) {
    const d = e.data || {};
    this.meta.textContent = "처리 중";
    switch (e.type) {
      case "run_start":
        if (!this.msg.textContent) this.msg.textContent = d.message || "";
        break;

      case "node_start":
        this.step(e.node);
        break;

      case "node_end": {
        const li = this.steps[e.node] || this.step(e.node);
        setState(li, d.ok ? "ok" : "fail");
        li._time.textContent = ms(d.elapsed_ms);
        li._info.textContent = this.summary(e.node, d);
        // 보정 내역은 계획 전체에 붙어 있으므로 최초 계획에서만 보여 준다
        if (e.node === "plan" && !d.round)
          (d.corrections || []).forEach((c) => this.note(c, "muted", li));
        break;
      }

      case "tool_request":
        this.call(d, "running");
        break;

      case "tool_response": {
        const li = this.calls[this.key(d)] || this.call(d, "running");
        li._time.textContent = ms(d.elapsed_ms);
        if (d.ok) setState(li, "ok");
        else this.failCall(li, d.code);
        break;
      }

      case "tool_reused": {
        const li = this.call(d, "ok", "재사용");
        if (!d.ok) this.failCall(li, d.code);
        break;
      }

      case "tool_skipped":
        this.failCall(this.call(d, "muted"), "SKIPPED");
        break;

      case "decision":
        this.decision(d);
        break;

      case "agent_message":
        this.message(d);
        break;

      case "run_end":
        this.end(d);
        break;
    }
  }

  summary(node, d) {
    if (node === "analyze") {
      if (d.error) return "분석 실패";
      return INTENT[d.intent] || d.intent || "";
    }
    if (node === "plan") {
      return (
        (d.steps || []).map((s) => TOOL[s.tool] || s.tool).join(" → ") ||
        "호출할 Tool 없음"
      );
    }
    if (node === "execute") {
      const parts = [`호출 ${d.called ?? 0}`];
      if (d.retried) parts.push(`재시도 ${d.retried}`);
      if (d.reused) parts.push(`재사용 ${d.reused}`);
      if (d.skipped) parts.push(`건너뜀 ${d.skipped}`);
      if (d.failed?.length) parts.push(`실패 ${d.failed.length}`);
      return parts.join(", ");
    }
    if (node === "judge") {
      return (d.judgments || [])
        .map((j) => `${j.item} ${JUDGMENT[j.status] || j.status}`)
        .join(", ");
    }
    return "";
  }

  decision(d) {
    if (d.kind === "intent") {
      const li = this.steps.analyze;
      if (li && d.reason) li.title = d.reason;
      if (d.by === "rule") this.note(d.reason, "warn", li);
    } else if (d.kind === "clarify") {
      this.note(
        "기준 조건이 부족해 Tool을 부르기 전에 되묻습니다.",
        "warn",
        this.steps.analyze,
      );
    } else if (d.kind === "replan") {
      this.replans = (this.replans || 0) + 1;
      const li = el("li", "replan");
      li.append(
        el(
          "span",
          "replan-title",
          `재계획 ${this.replans}, ${STRATEGY[d.choice] || d.choice}`,
        ),
        el("span", "replan-reason", d.reason || ""),
      );
      this.list.append(li);
    } else if (d.kind === "stop") {
      this.note(d.reason, "warn", this.steps.judge);
    } else if (d.kind === "worker") {
      // Worker의 판단은 실패한 호출 바로 아래에 붙인다. LLM 판단만 강조한다
      const what = WORKER_ACTION[d.choice] || d.choice;
      const args = d.args && Object.keys(d.args).length ? ` (${argsText(d.args)})` : "";
      this.note(
        `${d.by === "llm" ? "Worker LLM" : "Worker"}: ${what}${args}. ${d.reason || ""}`,
        d.by === "llm" ? "warn" : "muted",
        this.lastCall,
      );
    }
  }

  message(d) {
    // 작업 목록과 수행 보고는 단계와 호출 이력으로 이미 보이므로, 검증에서 이상이 나온 경우만 따로 알린다
    if (d.kind !== "verification" || d.body?.verdict !== "anomaly") return;
    for (const c of d.body.checks || []) {
      if (c.status === "warn")
        this.note(`로깅 Agent 검증: ${c.message}`, "warn", this.steps.judge);
    }
  }

  end(d) {
    this.finished = true;
    this.root.classList.remove("is-running");
    this.root
      .querySelectorAll(".is-running")
      .forEach((n) => setState(n, d.ok ? "ok" : "fail"));
    if (!d.ok) {
      this.failed = true;
      this.root.classList.add("is-fail");
    }
    const parts = [];
    if (d.replan_count)
      parts.push(`재계획 ${d.replan_count}/${d.replan_limit ?? "-"}`);
    parts.push(ms(d.elapsed_ms));
    this.meta.textContent = parts.join(", ");
  }

  // 응답을 받았는데 run_end를 못 받은 경우(서버 오류 등)
  abort(text) {
    if (this.finished) return;
    this.end({ ok: false });
    if (text) this.note(text, "fail", null);
  }

  review(r) {
    if (!r) return;
    // 이전에 저장된 판정은 실패한 실행도 normal로 남아 있다
    const verdict = this.failed && r.verdict === "normal" ? "failed" : r.verdict;
    const badge = el("span", `verdict is-${verdict}`, VERDICT[verdict] || verdict);
    if (r.warnings) badge.textContent += ` ${r.warnings}`;
    this.meta.prepend(badge);
    for (const c of r.checks || []) {
      if (c.status === "warn")
        this.note(`${c.label}: ${c.message}`, "warn", null);
    }
    if (r.diagnosis?.status === "ok" && r.diagnosis.text) {
      this.note(`진단: ${r.diagnosis.text}`, "muted", null);
    }
  }
}

// 저장된 이벤트를 요청(run) 단위로 나누어 그린다.
export function renderRuns(container, events, reviews = []) {
  container.replaceChildren();
  const byRun = new Map();
  for (const e of events) {
    if (!byRun.has(e.run_id)) byRun.set(e.run_id, []);
    byRun.get(e.run_id).push(e);
  }
  const reviewOf = new Map(reviews.map((r) => [r.run_id, r]));
  for (const [runId, list] of byRun) {
    const t = new RunTrace();
    list.forEach((e) => t.add(e));
    if (!t.finished) t.abort();
    t.review(reviewOf.get(runId));
    container.append(t.root);
  }
}
