# Frontend third-party notices

The production UI directly uses the following open-source packages. Exact versions and
all transitive dependencies are frozen in `package-lock.json`.

| Package | License | Purpose |
| --- | --- | --- |
| React / React DOM | MIT | UI runtime |
| TanStack Query | MIT | API state and cache |
| Zod | MIT | Runtime contract validation |
| XYFlow React | MIT | Workspace graph view |
| dnd-kit core / sortable / utilities | MIT | Accessible board interactions |
| Recharts | MIT | Workspace treemap view |

The development toolchain includes Vite, Vitest, Testing Library, jsdom, and their
dependencies under their respective package licenses. TypeScript is licensed under
Apache-2.0; the other direct development dependencies are MIT-licensed. Full license
texts are available in each installed package and its upstream repository.
