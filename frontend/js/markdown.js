// Agent 응답에 쓰는 Markdown만 다루는 작은 변환기 (제목, 목록, 표, 인용, 굵게, 코드, 링크, 표 안 <br>)

export function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inline(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
    )
    .replace(/&lt;br&gt;/g, "<br>");
}

function cells(row) {
  return row
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((c) => c.trim());
}

export function renderMarkdown(src) {
  const lines = String(src ?? "").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      const level = Math.min(h[1].length + 2, 6);
      out.push(`<h${level}>${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }
    if (line.trim().startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|"))
        rows.push(lines[i++]);
      const [head, , ...body] = rows;
      out.push(
        '<div class="table-wrap"><table><thead><tr>' +
          cells(head)
            .map((c) => `<th>${inline(c)}</th>`)
            .join("") +
          "</tr></thead><tbody>" +
          body
            .map(
              (r) =>
                "<tr>" +
                cells(r)
                  .map((c) => `<td>${inline(c)}</td>`)
                  .join("") +
                "</tr>",
            )
            .join("") +
          "</tbody></table></div>",
      );
      continue;
    }
    if (line.startsWith(">")) {
      const quote = [];
      while (i < lines.length && lines[i].startsWith(">"))
        quote.push(lines[i++].replace(/^>\s?/, ""));
      out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      continue;
    }
    const list = line.match(/^(\s*)(-|\d+\.)\s+/);
    if (list) {
      const ordered = list[2] !== "-";
      const items = [];
      while (i < lines.length && /^\s*(-|\d+\.)\s+/.test(lines[i])) {
        items.push(
          `<li>${inline(lines[i++].replace(/^\s*(-|\d+\.)\s+/, ""))}</li>`,
        );
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.join("")}</${tag}>`);
      continue;
    }
    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,4}\s|>|\||\s*(-|\d+\.)\s)/.test(lines[i])
    ) {
      para.push(lines[i++]);
    }
    out.push(`<p>${para.map(inline).join("<br>")}</p>`);
  }
  return out.join("");
}
