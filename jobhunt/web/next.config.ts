import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The local JSON backend reads a file outside the web/ folder.
  outputFileTracingIncludes: { "/": ["../data/**"] },
};

export default nextConfig;
