// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Which project the next conversation goes in. On the empty chat beside the
 * agent picker, and nowhere else: once a conversation exists, its project is
 * a fact about it.
 */
import type { Projects } from "../projects/projects";
import "./picker.css";

export function ProjectPicker({
  projects,
  chosen,
  onChoose,
}: Readonly<{
  projects: Projects;
  chosen: string | null;
  onChoose: (id: string) => void;
}>) {
  // Nothing to choose until the list has come; the backend's own default
  // is the "Default" project, so a message sent meanwhile still has a home.
  if (projects.status !== "ready" || projects.items.length === 0) return null;
  return (
    <label className="picker">
      <span>Project</span>
      <select
        value={chosen ?? ""}
        onChange={(event) => {
          onChoose(event.target.value);
        }}
        className="picker-select"
      >
        {projects.items.map((project) => (
          <option key={project.id} value={project.id}>
            {project.name}
          </option>
        ))}
      </select>
    </label>
  );
}
