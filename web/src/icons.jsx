// Small stroke icons (24-unit grid). Decorative unless a label is given.
const S = (props) => ({
  width: props.size || 16, height: props.size || 16, viewBox: "0 0 24 24", fill: "none",
  stroke: "currentColor", strokeWidth: props.w || 2.2, strokeLinecap: "round", strokeLinejoin: "round",
  "aria-hidden": props.label ? undefined : "true", role: props.label ? "img" : undefined, "aria-label": props.label,
});

export const Mic = (p) => (
  <svg {...S(p)}><path d="M9 5a3 3 0 0 1 6 0v6a3 3 0 0 1-6 0z" /><path d="M5 11a7 7 0 0 0 14 0" /><path d="M12 18v3" /></svg>
);
export const MicOff = (p) => (
  <svg {...S(p)}><path d="M9 5a3 3 0 0 1 6 0v6" /><path d="M9 9v2a3 3 0 0 0 5 2.2" /><path d="M5 11a7 7 0 0 0 10.6 6" /><path d="M19 11a7 7 0 0 1-1 3.6" /><path d="M12 18v3" /><path d="M4 4l16 16" /></svg>
);
export const Check = (p) => <svg {...S({ w: 3, ...p })}><path d="M5 12l5 5 9-10" /></svg>;
export const Alert = (p) => <svg {...S(p)}><path d="M12 4l9 16H3z" /><path d="M12 10v4" /><path d="M12 17.5v.5" /></svg>;
export const Play = (p) => <svg {...S(p)} fill="currentColor" stroke="none"><path d="M8 5v14l11-7z" /></svg>;
export const Stop = (p) => <svg {...S(p)} fill="currentColor" stroke="none"><rect x="5" y="5" width="14" height="14" rx="3" /></svg>;
export const Reset = (p) => <svg {...S(p)}><path d="M3 12a9 9 0 1 0 3-6.7" /><path d="M3 4v5h5" /></svg>;
export const Release = (p) => (
  <svg {...S(p)}><path d="M8 13V5a2 2 0 1 1 4 0v6" /><path d="M12 11V4a2 2 0 1 1 4 0v7" /><path d="M16 11V6a2 2 0 1 1 4 0v8a7 7 0 0 1-7 7h-1a7 7 0 0 1-6-3.3L3 14a2 2 0 0 1 3-2.6l2 2.6" /></svg>
);
export const Chat = (p) => <svg {...S(p)}><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z" /></svg>;
export const Lines = (p) => <svg {...S(p)}><path d="M4 6h16M4 12h16M4 18h10" /></svg>;
export const Close = (p) => <svg {...S(p)}><path d="M6 6l12 12" /><path d="M18 6L6 18" /></svg>;
export const Camera = (p) => (
  <svg {...S(p)}><path d="M3 8a2 2 0 0 1 2-2h3l2-2h4l2 2h3a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /><circle cx="12" cy="13" r="4" /></svg>
);

// The Buddy Bot mark: a two-link arm on a base, open gripper holding a ball. `tone` is the stroke
// color: black on the blue tile in the top bar, currentColor in the status rows.
export const Logo = ({ size = 22, tone = "#000" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={tone} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label="Buddy Bot">
    <path d="M3.5 21.5h8" /><path d="M7.5 18.5l2.5-8" /><path d="M10 10.5l6.5-3.5" />
    <circle cx="7.5" cy="18.5" r="2" fill={tone} /><circle cx="10" cy="10.5" r="1.6" fill={tone} />
    <path d="M16.5 7l3.5-3.5" /><path d="M16.5 7l4.5 1" /><circle cx="20.3" cy="5.8" r="1.1" fill={tone} stroke="none" />
  </svg>
);
