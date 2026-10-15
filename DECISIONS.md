# DECISIONS.md

## Indexes. :

### 1 `emp_code` [unique index on employees collection] = prevent duplicate employees creation and allow fast lookups by emp_code. 
### 2 `(department, emp_code)` [composite index on employees collection] =used for retrieving employees efficiently by filtering by department and emp_code.
### 3 `(joined_on)` [index on employees collection] = used to filter employees who joined or before the report month end.when calculating headcount for a department summary analytics.
### 4 `(emp_code, date)` [composite unique index on attendance_logs collection] = used to prevent duplicate punch-in logs for the same employee on the same date and allow fast lookups by emp_code and date(montly anlytics and department summary).
### 5 `(date, emp_code)` [composite index on attendance_logs collection] = Used to support attendance list sorting by date and the leaderboard's monthly date filtering.

## Two Identical Punch-in. :
 two punch-ins call insert_one,but the `(emp_code, date)` index only accepts one, the other raises duplicate key error (409).


## Ties. :

`$rank` window function assigns the same rank to tied values(eg:1,2,2,4) so filtering by rank <= 2 will return three employees instead of two.


## Headcount. :

headcount does't care whether the employees have attendance or groups. it just takes the employee who joined on or before the report `month end`.


## 100x. :

The `department summary` currently checks all attendance records for the monthe every time, someone requests the report(for 30,000 recods it takes 194 ms).if the data grows to 100 times that size, it would problem ,so the solution is to keep per-employee monthly totals, updated incrementally by each write instead of recomputed per query.