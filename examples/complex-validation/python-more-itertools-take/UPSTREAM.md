# Upstream provenance

- Repository: https://github.com/more-itertools/more-itertools
- Tag: `v11.2.1`
- Commit: `4aeb4338ae1407d8da9723e86da99efeb446f719`
- Source: `more_itertools/recipes.py`, function `take`
- License: MIT (the upstream `LICENSE` is retained beside this file)

This offline fixture is a deliberately small extraction of the upstream
`take(n, iterable)` recipe. Type annotations were added and the module was
renamed so the benchmark does not shadow an installed `more_itertools`
package. The intended upstream implementation passes `n` directly to
`itertools.islice`.

For repair evaluation only, the candidate source injects one boundary defect:
it passes `max(n - 1, 0)` instead. Tests retain the documented upstream
behavior for exact-length, short, and iterator inputs. The original upstream
repository and commit are never modified, and this fixture performs no network
access during verification.
