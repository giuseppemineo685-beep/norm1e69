import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Job hunt",
  description: "Ofertas del dia, decisiones y seguimiento de aplicaciones",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <head>
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;600&display=swap"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
