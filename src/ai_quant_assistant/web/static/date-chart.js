/* All positions are integer steps in the caller's date domain. */
function bindDateChart(svg, {length, fractionAt, render, select}) {
  let first = 0, last = length - 1, selected = null;
  const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
  function choose(index) {
    selected = clamp(index, first, last);
    select(selected);
  }
  function zoom(inward, fraction) {
    const span = last - first;
    const next = inward ? Math.max(1, Math.floor(span * 0.7))
                        : Math.min(length - 1, Math.ceil(span / 0.7));
    if (length < 3 || next === span) return false;
    first = clamp(first + Math.round((span - next) * fraction), 0, length - 1 - next);
    last = first + next;
    render(first, last);
    if (selected !== null) choose(selected);
    return true;
  }
  function fraction(event) {
    const matrix = svg.getScreenCTM();
    if (!matrix) return 0.5;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    return clamp(fractionAt(point.matrixTransform(matrix.inverse()).x, first, last), 0, 1);
  }
  svg.style.cursor = "crosshair";
  svg.setAttribute("tabindex", "0");
  svg.addEventListener("click", event => choose(first + Math.round(fraction(event) * (last - first))));
  svg.addEventListener("wheel", event => {
    if (event.ctrlKey || !event.deltaY) return;
    if (zoom(event.deltaY < 0, fraction(event))) event.preventDefault();
  }, {passive: false});
  svg.addEventListener("keydown", event => {
    if (event.target !== svg) return;
    if (event.key === "ArrowLeft") choose((selected ?? last + 1) - 1);
    else if (event.key === "ArrowRight") choose((selected ?? first - 1) + 1);
    else if (["+", "=", "-"].includes(event.key)) zoom(event.key !== "-", 0.5);
    else return;
    event.preventDefault();
  });
  return choose;
}
