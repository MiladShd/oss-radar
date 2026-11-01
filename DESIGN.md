# OSS Radar design system

The existing application is the visual authority. Shared tokens, header, fonts, components, and responsive rules live in `dashboard/app/static/design-system.css`, extracted from the incumbent inline stylesheet. The report loads this stylesheet; the existing application keeps its incumbent inline styles. The new Research navigation link uses the existing tab styling.

Preserve the established dark backgrounds (#080b10, #0d1218), panels (#10171f), light ink (#e8f0f5), muted text (#7d93a3), teal (#2ee6c4), amber (#ffb347), fine borders, 14px panel radius, Space Grotesk body/headings, and IBM Plex Mono data/navigation. Keep the radar brand mark and existing header structure. The evidence page adds only scoped report layout styles and uses shared CSS variables.

The current overview, score panels, model pages, source-health tables, and navigation remain intact. Research results live at `/evidence`, accessible through an added Research link; they do not replace the application homepage.

At small widths, navigation wraps, paired layouts stack, and evidence plots/tables scroll inside their own containers. Preserve source dates, unavailable states, and experimental labels. Do not redesign the existing overview as part of the Research page addition.
