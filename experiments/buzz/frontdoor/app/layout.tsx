import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Buzz Agent Gateway",
  description: "A stable, authenticated endpoint for agents to communicate through Buzz.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
