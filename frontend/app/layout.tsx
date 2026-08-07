import type { Metadata } from 'next';
import type { ReactNode } from 'react';

import Sidebar from '@/components/Sidebar';

import './globals.css';

export const metadata: Metadata = {
  title: 'ColdLineage',
  description:
    'Range-level data tiering, proved against every downstream consumer and written back to DataHub.',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <Sidebar />
          <main className="main">{children}</main>
        </div>
      </body>
    </html>
  );
}
