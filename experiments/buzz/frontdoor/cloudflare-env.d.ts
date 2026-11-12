declare namespace Cloudflare {
  interface Env {
    DB?: D1Database;
    BUCKET?: R2Bucket;
    BUZZ_ROUTING_KEY?: string;
    DOT_AGENT_TOKEN?: string;
    DOT_OWNER_EMAIL_HASH?: string;
  }
}
