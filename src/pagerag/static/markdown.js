/* 安全 Markdown 渲染：先转义全部文本，再识别标题、列表、表格、代码与引用角标。
   不解析原始 HTML，链接只允许 http 和 https。 */
(function () {
  const P = window.PageRAG;

  function inline(text, validRanks) {
    const codes = [];
    let html = P.escapeHtml(text);
    html = html.replace(/`([^`]+)`/g, (match, code) => {
      codes.push(`<code>${code}</code>`);
      return `\u0000CODE${codes.length - 1}\u0000`;
    });
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/\[(\d+)\]/g, (match, number) => {
      if (validRanks && !validRanks.has(Number(number))) return match;
      return `<button type="button" class="cite-ref" data-cite="${number}" `
        + `title="查看第 ${number} 条证据">[${number}]</button>`;
    });
    html = html.replace(/\u0000CODE(\d+)\u0000/g,
      (match, index) => codes[Number(index)] || "");
    return html;
  }

  function isTableSeparator(line) {
    return /\|/.test(line) && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line)
      && /-/.test(line);
  }

  function splitRow(line) {
    let row = line.trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|")) row = row.slice(0, -1);
    return row.split("|").map((cell) => cell.trim());
  }

  function collectList(lines, start, marker) {
    const items = [];
    let index = start;
    while (index < lines.length) {
      if (marker.test(lines[index])) {
        items.push(lines[index].replace(marker, ""));
        index += 1;
        continue;
      }
      // 模型常把列表项写成宽松列表，项与项之间隔着空行；这些空行不能
      // 把同一个列表切成多个 ol，否则每项都会重新从 1 开始编号。
      if (!lines[index].trim()) {
        let next = index + 1;
        while (next < lines.length && !lines[next].trim()) next += 1;
        if (next < lines.length && marker.test(lines[next])) {
          index = next;
          continue;
        }
      }
      break;
    }
    return { items, index };
  }

  function render(source, validRanks) {
    const text = String(source || "").replace(/\r\n?/g, "\n");
    const lines = text.split("\n");
    const out = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = line.match(/^\s*```(.*)$/);
      if (fence) {
        const code = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        index += 1;
        out.push(`<pre class="md-code"><code>${P.escapeHtml(code.join("\n"))}</code></pre>`);
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        const level = Math.min(6, heading[1].length + 2);
        out.push(`<h${level} class="md-heading">${inline(heading[2], validRanks)}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        out.push("<hr>");
        index += 1;
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        const quote = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          quote.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        out.push(`<blockquote>${inline(quote.join("\n"), validRanks)}</blockquote>`);
        continue;
      }

      if (line.includes("|") && index + 1 < lines.length
          && isTableSeparator(lines[index + 1])) {
        const header = splitRow(line);
        const rows = [];
        index += 2;
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          rows.push(splitRow(lines[index]));
          index += 1;
        }
        const head = header.map((cell) => `<th>${inline(cell, validRanks)}</th>`).join("");
        const body = rows.map((row) => {
          const cells = header.map((_, column) =>
            `<td>${inline(row[column] || "", validRanks)}</td>`).join("");
          return `<tr>${cells}</tr>`;
        }).join("");
        out.push(`<div class="md-table-wrap"><table><thead><tr>${head}</tr></thead>`
          + `<tbody>${body}</tbody></table></div>`);
        continue;
      }

      if (/^\s*[-*+]\s+/.test(line)) {
        const list = collectList(lines, index, /^\s*[-*+]\s+/);
        index = list.index;
        out.push(`<ul>${list.items.map((item) => `<li>${inline(item, validRanks)}</li>`).join("")}</ul>`);
        continue;
      }

      if (/^\s*\d+[.)]\s+/.test(line)) {
        const list = collectList(lines, index, /^\s*\d+[.)]\s+/);
        index = list.index;
        out.push(`<ol>${list.items.map((item) => `<li>${inline(item, validRanks)}</li>`).join("")}</ol>`);
        continue;
      }

      const paragraph = [];
      while (index < lines.length && lines[index].trim()
             && !/^\s*(```|#{1,6}\s|>|[-*+]\s|\d+[.)]\s)/.test(lines[index])) {
        if (paragraph.length && lines[index].includes("|")
            && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
          break;
        }
        paragraph.push(lines[index]);
        index += 1;
      }
      // 有些行会被上面的块起始判断排除，却没有匹配到任何具体块，例如
      // 不带空格的 #标题。此时必须把它当普通段落消费掉，否则索引不前进，
      // 整页渲染会在这里死循环。
      if (!paragraph.length) {
        paragraph.push(line);
        index += 1;
      }
      out.push(`<p>${inline(paragraph.join("\n"), validRanks)}</p>`);
    }
    return out.join("");
  }

  P.renderMarkdown = render;
}());
