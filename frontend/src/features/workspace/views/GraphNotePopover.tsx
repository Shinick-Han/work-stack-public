import { useEffect, useId } from "react";

import type { WorkspaceNote } from "./types";
import { noteTitle } from "./viewModels";
import "./GraphNotePopover.css";

interface GraphNotePopoverProps {
  note: WorkspaceNote;
  onClose: () => void;
}

/**
 * Read-only Graph note detail. It renders existing note fields only: no fetch,
 * no write, and no Task/Objective navigation.
 */
export function GraphNotePopover({ note, onClose }: GraphNotePopoverProps) {
  const titleId = useId();
  const links = note.links ?? [];
  const body = note.text || note.title || "";

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <aside
      className="wsv-graph-note"
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
    >
      <header className="wsv-graph-note__header">
        <div>
          <p>{note.id} · Note</p>
          <h2 id={titleId} title={noteTitle(note)}>{noteTitle(note)}</h2>
        </div>
        <button type="button" onClick={onClose} aria-label="Close note">Close</button>
      </header>
      <div className="wsv-graph-note__body">
        {note.created ? <p className="wsv-graph-note__created">{note.created}</p> : null}
        {body ? <p className="wsv-graph-note__text">{body}</p> : <p>This note has no body.</p>}
        <section aria-label="Note references">
          <h3>References</h3>
          {links.length ? (
            <ul>
              {links.map((link) => (
                <li key={link}>{link}</li>
              ))}
            </ul>
          ) : (
            <p>No references.</p>
          )}
        </section>
      </div>
    </aside>
  );
}
