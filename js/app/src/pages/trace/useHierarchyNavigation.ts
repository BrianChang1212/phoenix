import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type MouseEvent,
} from "react";

type HierarchyWindow = { start: number; end: number };
type Surface = "plot" | "overview";
type Gesture = {
  mode: "pan" | "brush" | "start" | "end";
  surface: Surface;
  element: Element;
  pointerId: number;
  origin: number;
  rect: DOMRect;
  bounds: HierarchyWindow;
  initial: HierarchyWindow;
  moved: boolean;
};
type Brush = {
  surface: Surface;
  left: number;
  width: number;
  containerWidth: number;
};

function boundedWindow(
  value: HierarchyWindow,
  full: HierarchyWindow
): HierarchyWindow {
  const minimum = Math.min(20, full.end - full.start);
  const width = Math.max(
    minimum,
    Math.min(full.end - full.start, value.end - value.start)
  );
  const start = Math.max(full.start, Math.min(full.end - width, value.start));
  return { start, end: start + width };
}

export function useHierarchyNavigation(
  full: HierarchyWindow,
  onEscape: () => void
) {
  const [requested, setRequested] = useState<HierarchyWindow | null>(null);
  const plotRef = useRef<HTMLDivElement>(null);
  const overviewRef = useRef<SVGSVGElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const suppressClick = useRef(false);
  const [brush, setBrush] = useState<Brush | null>(null);
  const view = boundedWindow(requested ?? full, full);
  const setWindow = useCallback(
    (value: HierarchyWindow) => {
      setRequested(boundedWindow(value, full));
    },
    [full.start, full.end]
  );
  const zoom = useCallback(
    (factor: number, anchor = 0.5) => {
      const width = view.end - view.start;
      const next = Math.max(
        Math.min(20, full.end - full.start),
        Math.min(full.end - full.start, width * factor)
      );
      const point = view.start + width * anchor;
      setWindow({
        start: point - next * anchor,
        end: point + next * (1 - anchor),
      });
    },
    [view.start, view.end, full.start, full.end, setWindow]
  );
  function pan(fraction: number) {
    const delta = (view.end - view.start) * fraction;
    setWindow({ start: view.start + delta, end: view.end + delta });
  }
  function reset() {
    gesture.current = null;
    setBrush(null);
    setRequested(null);
  }
  function begin(event: PointerEvent<Element>, surface: Surface) {
    if (event.button !== 0) return;
    const element = event.currentTarget;
    if (
      event.target instanceof Element &&
      event.target.closest(".hierarchy-name")
    )
      return;
    const rect =
      surface === "overview"
        ? element.getBoundingClientRect()
        : element.querySelector(".hierarchy-track")?.getBoundingClientRect();
    if (!rect || rect.width <= 0 || event.clientX < rect.left) return;
    const handle =
      event.target instanceof Element
        ? event.target
            .closest("[data-hierarchy-handle]")
            ?.getAttribute("data-hierarchy-handle")
        : null;
    const mode =
      handle === "start" || handle === "end"
        ? handle
        : surface === "overview" || event.shiftKey
          ? "brush"
          : "pan";
    if (element instanceof HTMLElement || element instanceof SVGElement)
      element.focus({ preventScroll: true });
    gesture.current = {
      mode,
      surface,
      element,
      pointerId: event.pointerId,
      origin: event.clientX,
      rect,
      initial: view,
      bounds: surface === "overview" ? full : view,
      moved: false,
    };
  }
  function move(event: PointerEvent<Element>) {
    const current = gesture.current;
    if (!current || current.pointerId !== event.pointerId) return;
    if (Math.abs(event.clientX - current.origin) > 4) {
      current.moved = true;
      current.element.setPointerCapture?.(event.pointerId);
    }
    if (!current.moved) return;
    suppressClick.current = true;
    const fraction = Math.max(
      0,
      Math.min(1, (event.clientX - current.rect.left) / current.rect.width)
    );
    const at =
      current.bounds.start +
      fraction * (current.bounds.end - current.bounds.start);
    if (current.mode === "start")
      setWindow({
        start: Math.min(
          at,
          current.initial.end - Math.min(20, full.end - full.start)
        ),
        end: current.initial.end,
      });
    else if (current.mode === "end")
      setWindow({
        start: current.initial.start,
        end: Math.max(
          at,
          current.initial.start + Math.min(20, full.end - full.start)
        ),
      });
    else if (current.mode === "pan") {
      const delta =
        (-(event.clientX - current.origin) / current.rect.width) *
        (current.initial.end - current.initial.start);
      setWindow({
        start: current.initial.start + delta,
        end: current.initial.end + delta,
      });
    } else {
      const rect = current.element.getBoundingClientRect();
      setBrush({
        surface: current.surface,
        left: Math.min(current.origin, event.clientX) - rect.left,
        width: Math.abs(current.origin - event.clientX),
        containerWidth: rect.width,
      });
    }
  }
  const finish = useCallback(
    (event: { pointerId: number; clientX: number }, cancel = false) => {
      const current = gesture.current;
      if (!current || current.pointerId !== event.pointerId) return;
      gesture.current = null;
      if (current.element.hasPointerCapture?.(event.pointerId))
        current.element.releasePointerCapture(event.pointerId);
      setBrush(null);
      if (current.moved && current.mode === "brush" && !cancel) {
        const origin = Math.max(
          0,
          Math.min(1, (current.origin - current.rect.left) / current.rect.width)
        );
        const end = Math.max(
          0,
          Math.min(1, (event.clientX - current.rect.left) / current.rect.width)
        );
        const width = current.bounds.end - current.bounds.start;
        setWindow({
          start: current.bounds.start + Math.min(origin, end) * width,
          end: current.bounds.start + Math.max(origin, end) * width,
        });
      }
      setTimeout(() => {
        suppressClick.current = false;
      }, 0);
    },
    [setWindow]
  );
  useEffect(() => {
    const end = (event: globalThis.PointerEvent) => finish(event);
    window.addEventListener("pointerup", end);
    return () => window.removeEventListener("pointerup", end);
  }, [finish]);
  function blockDragClick(event: MouseEvent<Element>) {
    if (suppressClick.current) {
      event.preventDefault();
      event.stopPropagation();
    }
  }
  function keyDown(event: KeyboardEvent<HTMLElement>) {
    if (
      event.ctrlKey ||
      event.metaKey ||
      event.altKey ||
      (event.target instanceof Element &&
        event.target.closest('input,textarea,select,[contenteditable="true"]'))
    )
      return;
    if (
      [
        "+",
        "=",
        "-",
        "_",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "Escape",
      ].includes(event.key)
    )
      event.preventDefault();
    if (event.key === "+" || event.key === "=") zoom(0.5);
    if (event.key === "-" || event.key === "_") zoom(2);
    if (event.key === "ArrowLeft") pan(-0.2);
    if (event.key === "ArrowRight") pan(0.2);
    if (event.key === "Home") reset();
    if (event.key === "Escape") onEscape();
  }
  useEffect(() => {
    const plot = plotRef.current;
    const overview = overviewRef.current;
    const onPlotWheel = (event: WheelEvent) => {
      const rect = plot
        ?.querySelector(".hierarchy-track")
        ?.getBoundingClientRect();
      if (!rect || rect.width <= 0 || event.clientX < rect.left) return;
      event.preventDefault();
      zoom(
        Math.exp(Math.max(-1, Math.min(1, event.deltaY * 0.002))),
        Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width))
      );
    };
    const onOverviewWheel = (event: WheelEvent) => {
      const rect = overview?.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;
      event.preventDefault();
      zoom(
        Math.exp(Math.max(-1, Math.min(1, event.deltaY * 0.002))),
        Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width))
      );
    };
    plot?.addEventListener("wheel", onPlotWheel, { passive: false });
    overview?.addEventListener("wheel", onOverviewWheel, { passive: false });
    return () => {
      plot?.removeEventListener("wheel", onPlotWheel);
      overview?.removeEventListener("wheel", onOverviewWheel);
    };
  }, [zoom]);
  return {
    view,
    full,
    setWindow,
    zoom,
    pan,
    reset,
    plotRef,
    overviewRef,
    keyDown,
    brush,
    plotEvents: {
      onPointerDown: (event: PointerEvent<Element>) => begin(event, "plot"),
      onPointerMove: move,
      onPointerUp: (event: PointerEvent<Element>) => finish(event),
      onPointerCancel: (event: PointerEvent<Element>) => finish(event, true),
      onClickCapture: blockDragClick,
    },
    overviewEvents: {
      onPointerDown: (event: PointerEvent<Element>) => begin(event, "overview"),
      onPointerMove: move,
      onPointerUp: (event: PointerEvent<Element>) => finish(event),
      onPointerCancel: (event: PointerEvent<Element>) => finish(event, true),
      onClickCapture: blockDragClick,
    },
  };
}

export type HierarchyNavigation = ReturnType<typeof useHierarchyNavigation>;
