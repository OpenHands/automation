import {
  type RouteConfig,
  layout,
  index,
  route,
} from "@react-router/dev/routes";

export default [
  layout("routes/root-layout.tsx", [
    index("routes/automations-list.tsx"),
    route("arch", "routes/architecture.tsx"),
    route(":automationId", "routes/automation-detail.tsx"),
  ]),
] satisfies RouteConfig;
