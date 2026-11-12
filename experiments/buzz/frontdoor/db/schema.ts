import { sqliteTable, text } from "drizzle-orm/sqlite-core";

export const gatewayRoutes = sqliteTable("gateway_routes", {
  id: text("id").primaryKey(),
  upstream: text("upstream").notNull(),
  tokenHashes: text("token_hashes").notNull(),
  updatedAt: text("updated_at").notNull(),
});
