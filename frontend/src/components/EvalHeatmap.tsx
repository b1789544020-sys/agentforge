import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Evals, type EvalRun } from "../api/client";

/**
 * 0.1.3 #4 -- small inline SVG heatmap.
 *
 * Renders one column per evaluation run and one row per unique test
 * case (matched by ``input`` text).  Each cell is coloured by score
 * (red 0 -> green 1); cells with an ``error`` show a red âœ• glyph.
 *
 * The component is self-contained (no external charting deps) and
 * fetches per-run case details lazily on mount.
 */
export interface EvalHeatmapProps {
  agentId: string;
  runs: EvalRun[];
  /** Optional cap on how many runs to render; defaults to 8. */
  maxRuns?: number;
}

interface Cell {
  score: number | null;
  passed: boolean | null;
  error?: string | null;
  input: string;
  expected?: string;
}

interface Row {
  /** Stable row key -- use the first matching run's input as a key. */
  key: string;
  input: string;
  expected?: string;
  cells: (Cell | null)[]; // one entry per run, in render order
}

const SCORE_COLOURS = [
  "#d73027", // 0.0  red
  "#fc8d59", // 0.25 orange
  "#fee08b", // 0.5  yellow
  "#d9ef8b", // 0.75 lime
  "#91cf60", // 1.0  green
] as const;

function scoreToColour(score: number | null): string {
  if (score === null || Number.isNaN(score)) return "#cccccc";
  const clamped = Math.max(0, Math.min(1, score));
  const idx = Math.min(
    SCORE_COLOURS.length - 1,
    Math.floor(clamped * SCORE_COLOURS.length)
  );
  return SCORE_COLOURS[idx];
}

function cellTextColour(score: number | null): string {
  if (score === null) return "#222";
  return score > 0.55 ? "#0b1a0b" : "#fff";
}

export default function EvalHeatmap({ agentId, runs, maxRuns = 8 }: EvalHeatmapProps) {
  const { t } = useTranslation();
  // Limit to the most recent N runs (already ordered newest-first in
  // the parent component).
  const visibleRuns = useMemo(() => runs.slice(0, maxRuns), [runs, maxRuns]);
  const [hydrated, setHydrated] = useState<EvalRun[]>([]);
  const [loading, setLoading] = useState(false);

  // Fetch per-run case details for any visible run that doesn't have
  // them already.  Done in a single effect that fan-outs concurrently.
  useEffect(() => {
    let cancelled = false;
    const need = visibleRuns.filter((r) => !r.cases);
    if (need.length === 0) {
      setHydrated(visibleRuns);
      return () => {
        cancelled = true;
      };
    }
    setLoading(true);
    Promise.all(need.map((r) => Evals.get(agentId, r.id)))
      .then((fresh) => {
        if (cancelled) return;
        const byId = new Map<string, EvalRun>(fresh.map((r) => [r.id, r]));
        setHydrated(
          visibleRuns.map((r) => byId.get(r.id) ?? r)
        );
      })
      .catch(() => {
        // Non-fatal -- we'll just render whatever cases the parent had.
        if (!cancelled) setHydrated(visibleRuns);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, visibleRuns]);

  // Build the row -> cells matrix.  Row key = case input text.
  const rows: Row[] = useMemo(() => {
    const rowMap = new Map<string, Row>();
    for (const run of hydrated) {
      const cases = run.cases ?? [];
      for (const c of cases) {
        const key = c.input || "(empty)";
        let row = rowMap.get(key);
        if (!row) {
          row = { key, input: c.input, expected: c.expected, cells: [] };
          rowMap.set(key, row);
        }
      }
    }
    for (const run of hydrated) {
      const cases = run.cases ?? [];
      // Re-walk in the same row order so cells align column-wise.
      for (const row of rowMap.values()) {
        const found = cases.find((c) => (c.input || "(empty)") === row.key);
        if (found) {
          row.cells.push({
            score: typeof found.score === "number" ? found.score : null,
            passed: !!found.passed,
            error: found.error ?? null,
            input: found.input,
            expected: found.expected,
          });
        } else {
          row.cells.push(null);
        }
      }
    }
    return Array.from(rowMap.values());
  }, [hydrated]);

  if (visibleRuns.length === 0) {
    return (
      <div className="heatmap-empty">{t("eval.heatmap_empty")}</div>
    );
  }

  const cellSize = 28;
  const labelWidth = 220;
  const headerHeight = 56;
  const totalWidth = labelWidth + cellSize * visibleRuns.length;
  const totalHeight = headerHeight + cellSize * Math.max(1, rows.length);

  return (
    <div className="heatmap-wrapper">
      <div className="heatmap-header">
        <h3>{t("eval.heatmap_title")}</h3>
        <div className="heatmap-legend" aria-hidden="true">
          <span>{t("eval.heatmap_legend_low")}</span>
          {SCORE_COLOURS.map((c) => (
            <span key={c} className="heatmap-legend-swatch" style={{ background: c }} />
          ))}
          <span>{t("eval.heatmap_legend_high")}</span>
        </div>
      </div>
      <p className="muted small">{t("eval.heatmap_subtitle")}</p>
      {loading ? <div className="muted small">¡­</div> : null}
      <div className="heatmap-scroll">
        <svg
          role="img"
          aria-label={t("eval.heatmap_title")}
          width={totalWidth}
          height={totalHeight}
          viewBox={`0 0 ${totalWidth} ${totalHeight}`}
        >
          {/* Column headers: dataset name + scorer, rotated 45deg */}
          {visibleRuns.map((run, col) => (
            <g
              key={run.id}
              transform={`translate(${labelWidth + col * cellSize + cellSize / 2}, 0)`}
            >
              <text
                transform="rotate(-45)"
                x={0}
                y={headerHeight - 6}
                textAnchor="start"
                fontSize="11"
                fill="currentColor"
              >
                {`${run.dataset_name} ¡¤ ${run.scorer}`}
              </text>
            </g>
          ))}
          {/* Rows */}
          {rows.map((row, rowIdx) => (
            <g key={row.key} transform={`translate(0, ${headerHeight + rowIdx * cellSize})`}>
              <text
                x={labelWidth - 8}
                y={cellSize / 2 + 4}
                textAnchor="end"
                fontSize="12"
                fill="currentColor"
              >
                {row.input.length > 30 ? `${row.input.slice(0, 28)}¡­` : row.input}
              </text>
              {row.cells.map((cell, col) => {
                if (!cell) {
                  return (
                    <rect
                      key={col}
                      x={labelWidth + col * cellSize}
                      y={0}
                      width={cellSize}
                      height={cellSize}
                      fill="#f5f5f5"
                      stroke="#fff"
                    />
                  );
                }
                const isError = !!cell.error;
                return (
                  <g key={col}>
                    <title>
                      {t("eval.heatmap_cell_title", {
                        dataset: visibleRuns[col]?.dataset_name ?? "",
                        case: row.input,
                        score:
                          cell.score === null
                            ? "¡ª"
                            : (cell.score * 100).toFixed(0) + "%",
                        passed: cell.passed ? "?" : "?",
                      })}
                    </title>
                    <rect
                      x={labelWidth + col * cellSize}
                      y={0}
                      width={cellSize}
                      height={cellSize}
                      fill={isError ? "#b00020" : scoreToColour(cell.score)}
                      stroke="#fff"
                    />
                    {isError ? (
                      <text
                        x={labelWidth + col * cellSize + cellSize / 2}
                        y={cellSize / 2 + 5}
                        textAnchor="middle"
                        fontSize="14"
                        fontWeight={700}
                        fill="#fff"
                      >
                        ?
                      </text>
                    ) : (
                      <text
                        x={labelWidth + col * cellSize + cellSize / 2}
                        y={cellSize / 2 + 4}
                        textAnchor="middle"
                        fontSize="10"
                        fill={cellTextColour(cell.score)}
                      >
                        {cell.score === null
                          ? "¡ª"
                          : (cell.score * 100).toFixed(0)}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>
          ))}
        </svg>
      </div>
    </div>
  );
}
