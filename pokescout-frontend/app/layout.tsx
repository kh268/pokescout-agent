import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "PokéScout — Your next great find",
  description: "Explore Pokémon cards, build an investment strategy, and complete your dream collection. An Agent Forge demo.",
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
