# Service performance release reachability review

The release starts at fresh origin/main. All 171 retained disposition source lines match their previous source bytes. The logging getattr line moved from 684 to 685. Its expression and declared targets are unchanged. The scanner is unchanged. Nine new bounded Node subprocess calls are confined to synthetic lifecycle tests. Their exact source positions are asserted. No production disposition or target was added.

The source hash and closure checks below bind this release candidate after final static validation.

Native Windows follow-up: one existing transcript fixture now writes exact UTF-8 bytes, so its raw byte count does not depend on platform newline translation. All 171 retained fingerprints, dispositions, and targets remain unchanged. Aggregate source provenance is `3e8aa49657b89d63b855bd9125c9ee0ef1847d37176eec6a30346de583e67251`. No application code or scanner changed.
