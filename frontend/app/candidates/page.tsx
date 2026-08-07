import { Suspense } from 'react';

import CandidateWorkbench from '@/components/CandidateWorkbench';
import { LoadingState } from '@/components/Primitives';

/* The workbench reads the selected dataset id out of the query string, so it
   needs a Suspense boundary for the App Router's prerender pass. */
export default function CandidatesPage() {
  return (
    <Suspense fallback={<LoadingState label="Loading the range simulator…" />}>
      <CandidateWorkbench />
    </Suspense>
  );
}
