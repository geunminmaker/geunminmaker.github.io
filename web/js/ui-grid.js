// Markup for everything that draws a grid over (or out of) the picture:
// the dividing lines, the numbered cells, the small layout pictograms and the result mosaic.
// Tiles are numbered row by row, left to right, starting at 1 - the same everywhere.

/** How much room a number badge has: "" (normal), "tight" (small badge) or "none" (hidden). */
export function density(cols, rows) {
  if (cols >= 16 || rows >= 12) return "none";
  if (cols >= 10 || rows >= 8) return "tight";
  return "";
}

/** White lines on a dark casing, so they show on any picture. */
export function gridLines(cols, rows) {
  let lines = "";
  for (let i = 1; i < cols; i += 1) {
    const x = ((100 * i) / cols).toFixed(3);
    lines += `<line x1="${x}" y1="0" x2="${x}" y2="100"/>`;
  }
  for (let i = 1; i < rows; i += 1) {
    const y = ((100 * i) / rows).toFixed(3);
    lines += `<line x1="0" y1="${y}" x2="100" y2="${y}"/>`;
  }
  return '<svg class="gridlines" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
    + `<g class="gl-case">${lines}</g><g class="gl-line">${lines}</g></svg>`;
}

/** Centred number badges, one per tile. */
export function gridCells(cols, rows) {
  const kind = density(cols, rows);
  let cells = "";
  for (let i = 1; i <= cols * rows; i += 1) cells += `<span><b>${i}</b></span>`;
  return `<div class="cells${kind ? ` cells--${kind}` : ""}" aria-hidden="true" `
    + `data-css="grid-template-columns:repeat(${cols},1fr);grid-template-rows:repeat(${rows},1fr)">${cells}</div>`;
}

/** Dark veil over tiles that are not made yet (the work screen clears it tile by tile). */
export function veil(cols, rows) {
  return `<div class="veil" aria-hidden="true" data-css="grid-template-columns:repeat(${cols},1fr);grid-template-rows:repeat(${rows},1fr)">`
    + "<i></i>".repeat(cols * rows) + "</div>";
}

/** Pictogram of a layout, drawn in the picture's own proportions (52 x 40 box). */
export function miniLayout(cols, rows, aspect) {
  let width = 52;
  let height = 52 / aspect;
  if (height > 40) {
    height = 40;
    width = 40 * aspect;
  }
  width = Math.max(width, 16);
  height = Math.max(height, 16);
  const x0 = (52 - width) / 2;
  const y0 = (40 - height) / 2;
  let path = "";
  for (let i = 1; i < cols; i += 1) {
    path += `M${(x0 + (width * i) / cols).toFixed(2)} ${y0.toFixed(2)}V${(y0 + height).toFixed(2)}`;
  }
  for (let i = 1; i < rows; i += 1) {
    path += `M${x0.toFixed(2)} ${(y0 + (height * i) / rows).toFixed(2)}H${(x0 + width).toFixed(2)}`;
  }
  return '<svg viewBox="0 0 52 40" aria-hidden="true">'
    + `<rect class="lg-box" x="${x0.toFixed(2)}" y="${y0.toFixed(2)}" width="${width.toFixed(2)}" height="${height.toFixed(2)}" rx="3"/>`
    + `<path class="lg-line" d="${path}"/></svg>`;
}

/**
 * The finished pieces laid out as they were cut, with small gaps.
 * Every tile is a window onto the one preview image (CSS background), so nothing is asked of the engine.
 * The parent sets --img, --ar (picture aspect) and --maxh.
 */
export function mosaic(cols, rows, tileAspect, label) {
  const kind = density(cols, rows) || (cols >= 6 || rows >= 6 ? "snug" : "");
  let tiles = "";
  for (let i = 0; i < cols * rows; i += 1) {
    const x = cols > 1 ? ((100 * (i % cols)) / (cols - 1)).toFixed(3) : "0";
    const y = rows > 1 ? ((100 * Math.floor(i / cols)) / (rows - 1)).toFixed(3) : "0";
    tiles += `<i data-css="background-position:${x}% ${y}%"><b>${i + 1}</b></i>`;
  }
  return `<div class="mosaic${kind ? ` mosaic--${kind}` : ""}" role="img" aria-label="${label}" `
    + `data-css="--c:${cols};--r:${rows};--tar:${tileAspect.toFixed(5)}">${tiles}</div>`;
}
