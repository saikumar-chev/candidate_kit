# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Used naive local time and calculated against the machine timezone. | A punch near midnight or a machine outside IST gives the wrong late value. | Store whole-second UTC instants and calculate against the employee's IST shift. |
| 2 | `compute_work_hours` | Python `round` uses binary floating-point/banker's rounding. | A duration exactly at a half-cent boundary returns the wrong reported value. | Use `Decimal` with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Did not handle overnight shifts and compared aware/naive datetimes inconsistently. | The sample overnight record reports no overtime or raises a datetime error. | Build the shift end on the following calendar day for overnight shifts. |
| 4 | `health` | Always returned 200 without checking MongoDB. | Stop MongoDB and the readiness probe still says the service is ready. | Ping MongoDB and return 503 on failure. |
| 5 | `create_employee` | Check-then-insert allowed concurrent duplicate employee codes; it also returned a BSON datetime. | Concurrent requests can both succeed, and the response violates the epoch-millisecond contract. | Add a unique index, catch `DuplicateKeyError`, and serialize timestamps. |
| 6 | `list_employees` | Skipped `page * page_size`, counted all departments, and did not sort or validate page bounds. | Page 1 omits its first page and filtered totals are too large. | Apply the filter to the count, use `(page - 1) * page_size`, sort, and validate limits. |
| 7 | `punch_in` | Did not reject an unknown employee, did not enforce allowed statuses, used local time, and used a non-atomic duplicate check. | Unknown employees fail with an internal error; WFH/ON_DUTY and duplicate races behave incorrectly. | Validate the request, normalize IST/UTC correctly, and rely on the unique attendance index. |
| 8 | `list_attendance` | Loaded and sorted the whole collection in Python, leaked `_id` as an `id`, and did not apply indexed pagination. | A large dataset is slow and the response has fields absent from the contract. | Filter, sort, skip, and limit in MongoDB and serialize only contract fields. |
| 9 | Missing contract endpoints | Punch-out, corrections, analytics, and explain were absent. | Requests to those paths return 404. | Implemented the remaining contract paths. |

The original helper names were retained conceptually, but the implementation was consolidated because their old datetime assumptions could not safely support the contract's IST and epoch rules. I did not treat the absence of authentication as a defect: the assignment explicitly makes `regularized_by` free text and provides no authentication requirement.

Also note anything you looked at and decided was **not** a defect, and why.
