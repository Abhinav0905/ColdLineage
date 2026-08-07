import './globals.css'
import Link from 'next/link'
export const metadata={title:'ColdLineage',description:'Agentic control plane for governed data tiering'}
export default function Layout({children}:{children:React.ReactNode}){return <html lang="en"><body><div className="shell"><aside className="side"><div className="brand">ColdLineage</div><div className="tag">Keep the context hot. Move the data cold.</div><nav className="nav"><Link href="/">Overview</Link><Link href="/candidates">Candidates</Link><Link href="/restore">Restore</Link><Link href="/audit">Audit</Link></nav><div style={{position:'absolute',bottom:24,left:16,right:16}} className="muted">DataHub-native agentic lifecycle control</div></aside><main className="main">{children}</main></div></body></html>}
