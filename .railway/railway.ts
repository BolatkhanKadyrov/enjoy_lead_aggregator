import { defineRailway, github, preserve, project, service, volume } from "railway/iac";

export const partial = "enjoy_lead_aggregator";

export default defineRailway(() => {
  const data = volume("enjoy_lead_aggregator-volume", { region: "ams", sizeMB: 5000 });
  const bot = service("enjoy_lead_aggregator", {
    source: github("BolatkhanKadyrov/enjoy_lead_aggregator", { branch: "main" }),
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "/Dockerfile",
    },
    deploy: {
      healthcheckPath: "/health",
      healthcheckTimeout: 60,
      // Railway represents its ON_FAILURE / 10 retries policy by omitting both fields.
    },
    env: {
      ADMIN_IDS: preserve(),
      BOT_TOKEN: preserve(),
      DATA_DIR: preserve(),
      INITIAL_CATALOG_JSON: preserve(),
      OWNER_ID: preserve(),
      PORT: preserve(),
    },
    networking: {
      privateNetworkEndpoint: "enjoyleadaggregator",
      serviceDomains: {
        "enjoyleadaggregator-production.up.railway.app": { port: 8080 },
      },
    },
    volumeMounts: { "/data": data },
  });

  return project("vigilant-quietude", { resources: [bot] });
});
