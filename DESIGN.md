# OSS Radar design system

The existing application is the visual authority. Shared tokens, header, fonts, components, and responsive rules live in `dashboard/app/static/design-system.css`, extracted from the incumbent inline stylesheet. Both `/` and the separate `/evidence` page load this same file.

Preserve the established dark backgrounds (#080b10, #0d1218), panels (#10171f), light ink (#e8f0f5), muted text (#7d93a3), teal (#2ee6c4), amber (#ffb347), fine borders, 14px panel radius, Space Grotesk body/headings, and IBM Plex Mono data/navigation. Keep the radar brand mark and existing header structure. The evidence page adds only scoped report layout styles and uses shared CSS variables.

The main overview leads with observed download changes and explicit dependency findings. Source-health tables are disclosed on demand. Models, accuracy, validation, operating logs, and activity remain accessible under Technical details. Research results live at `/evidence`; they do not replace the application homepage.

At small widths, navigation wraps, paired layouts stack, and evidence plots/tables scroll inside their own containers. Preserve source dates, unavailable states, and experimental labels. Avoid blended score KPIs in the main overview.
