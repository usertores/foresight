import { highlight, snippet } from "./markdown";

type HitLike = {
  title: string;
  source: string;
  section: string;
  page: number;
  text: string;
};

/** CSS class for a source label: one of six colours, the same one every time for a name. */
export function sourceClass(source: string) {
  let h = 0;
  for (const c of source) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return `src-${h % 6}`;
}

/** A library passage: source badge, title, page, chapter path and a highlighted snippet. */
export function HitCard(props: {
  hit: HitLike;
  idx?: number;
  num?: number;
  query?: string;
  selected?: boolean;
  compact?: boolean;
  onOpen: () => void;
  onHover?: () => void;
}) {
  const { hit: h, idx, num, query = "", selected, compact, onOpen, onHover } = props;
  return (
    <article
      data-idx={idx}
      className={`hit card ${selected ? "selected" : ""} ${compact ? "compact" : ""}`}
      onClick={onOpen}
      onMouseEnter={onHover}
      title="Open at this page"
    >
      <div className="hit-head">
        {num !== undefined && <span className="num">{num}</span>}
        <span className={`badge ${sourceClass(h.source)}`}>{h.source}</span>
        <span className="title">{h.title}</span>
        {h.page > 0 && <span className="page">p. {h.page}</span>}
      </div>
      {h.section && <div className="section">{h.section}</div>}
      <p
        className="text"
        dangerouslySetInnerHTML={{ __html: highlight(snippet(h.text, query, compact ? 240 : 420), query) }}
      />
    </article>
  );
}
