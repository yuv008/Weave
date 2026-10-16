(() => {
  if (document.getElementById("weave-wisp-companion")) return;

  const frame = document.createElement("iframe");
  frame.id = "weave-wisp-companion";
  frame.title = "Wisp";
  frame.src = "./wisp-frame.html";
  frame.setAttribute("scrolling", "no");
  Object.assign(frame.style, {
    position: "fixed",
    width: "156px",
    height: "104px",
    border: "0",
    margin: "0",
    padding: "0",
    background: "transparent",
    colorScheme: "normal",
    zIndex: "10050",
    pointerEvents: "auto",
    display: "none",
    overflow: "hidden",
  });
  document.body.appendChild(frame);

  const place = () => {
    const boxes = [...document.querySelectorAll(".ai-input-full-input-box")].filter((el) => {
      const rect = el.getBoundingClientRect();
      return rect.width > 80 && rect.height > 20 && rect.bottom > 40 && rect.right > 40;
    });
    if (!boxes.length) {
      frame.style.display = "none";
      return;
    }
    boxes.sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right);
    const rect = boxes[0].getBoundingClientRect();
    const margin = 10;
    const width = Math.max(96, Math.min(156, rect.width - margin * 2, window.innerWidth - margin * 2));
    const height = 104;
    let left = rect.right - width - margin;
    let top = rect.top - height + 12;
    left = Math.max(rect.left + margin, margin, Math.min(left, window.innerWidth - width - margin));
    top = Math.max(margin, Math.min(top, window.innerHeight - height - margin));
    frame.style.display = "block";
    frame.style.width = Math.round(width) + "px";
    frame.style.height = height + "px";
    frame.style.left = Math.round(left) + "px";
    frame.style.top = Math.round(top) + "px";
  };

  place();
  setInterval(place, 300);
  window.addEventListener("resize", place);
})();
