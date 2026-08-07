'use client';

import { Layers, ScrollText, Snowflake, Target, Undo2 } from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

import DataHubStatusChip from '@/components/DataHubStatusChip';
import { API_BASE } from '@/lib/api';

const NAV = [
  { href: '/', label: 'Overview', Icon: Layers },
  { href: '/candidates', label: 'Candidates', Icon: Target },
  { href: '/restore', label: 'Restore', Icon: Undo2 },
  { href: '/audit', label: 'Audit', Icon: ScrollText },
];

export default function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <Snowflake size={18} />
        </span>
        <span>
          <span className="brand-name">ColdLineage</span>
          <span className="brand-sub">Keep the context hot. Move the data cold.</span>
        </span>
      </div>

      <nav className="nav" aria-label="Primary">
        {NAV.map(({ href, label, Icon }) => {
          const active = href === '/' ? pathname === '/' : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className="nav-link"
              aria-current={active ? 'page' : undefined}
            >
              <Icon size={16} aria-hidden="true" />
              {label}
            </Link>
          );
        })}
      </nav>

      <div className="sidebar-foot">
        <DataHubStatusChip />
        <div className="muted" style={{ fontSize: 13, fontFamily: 'var(--mono)', overflowWrap: 'anywhere' }}>
          api {API_BASE}
        </div>
      </div>
    </aside>
  );
}
