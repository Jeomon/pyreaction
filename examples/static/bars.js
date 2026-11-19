// A tiny bar chart drawn with plain DOM, to show the js_component contract.
export function mount(element, props, emit) {
  const draw = ({ values = [], color = "steelblue" }) => {
    element.replaceChildren();
    element.style.cssText = "display:flex;align-items:flex-end;gap:4px;height:120px";
    const max = Math.max(1, ...values);
    values.forEach((v, index) => {
      const bar = document.createElement("button");
      bar.title = String(v);
      bar.style.cssText = `width:28px;height:${(v / max) * 100}%;background:${color};border:0;cursor:pointer`;
      bar.onclick = () => emit("pick", { index, value: v });
      element.appendChild(bar);
    });
  };
  draw(props);
  return { update: draw, unmount: () => element.replaceChildren() };
}
