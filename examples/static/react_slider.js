// Wrapping a React component for js_component(): React draws into a box of its own,
// props from Python are passed through, and callbacks reach Python through emit().
//
//     Slider = js_component("/static/react_slider.js", "mount")
//     Slider(value=volume, max=100, on_change=lambda e: set_volume(e.value))
//
// React and ReactDOM load from a CDN as ES modules; pin the versions you test with.
import React from "https://esm.sh/react@19.3.1";
import { createRoot } from "https://esm.sh/react-dom@19.3.1/client";

function Slider({ value, max, onChange }) {
  return React.createElement(
    "label",
    { style: { display: "flex", gap: "0.5rem", alignItems: "center" } },
    React.createElement("input", {
      type: "range",
      min: 0,
      max,
      value,
      onChange: (e) => onChange(Number(e.target.value)),
    }),
    React.createElement("output", null, value),
  );
}

export function mount(element, props, emit, slot) {
  // React replaces the contents of the node it renders into, so give it its own
  // box and keep pyreaction's slot (Python children) beside it.
  const box = document.createElement("div");
  element.insertBefore(box, slot);
  const root = createRoot(box);
  const render = (p) =>
    root.render(
      React.createElement(Slider, {
        value: p.value ?? 0,
        max: p.max ?? 100,
        onChange: (v) => emit("change", { value: v }),
      }),
    );
  render(props);
  return { update: render, unmount: () => root.unmount() };
}
