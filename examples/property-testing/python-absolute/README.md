# Differential property-check fixture

The project compiles, but `absolute()` violates its non-negative result invariant for negative integers. `candidate_property.py` is deliberately kept outside the project and is never copied into the original test tree.

After installing Hypothesis, preview the executable candidate first:

```powershell
python -m pip install hypothesis
autofix property-check .\examples\property-testing\python-absolute\candidate_property.py `
  --project .\examples\property-testing\python-absolute\project `
  --plan .\examples\property-testing\python-absolute\repair-plan.json
```

Then request isolated differential execution:

```powershell
autofix property-check .\examples\property-testing\python-absolute\candidate_property.py `
  --project .\examples\property-testing\python-absolute\project `
  --plan .\examples\property-testing\python-absolute\repair-plan.json `
  --run
```

A successful protocol verdict is `counterexample-confirmed`: two stable failures on the original implementation, two passes after applying the reviewed plan in a separate copy, and a passing original verification matrix. Neither `calculator.py` nor a project test file is changed.
