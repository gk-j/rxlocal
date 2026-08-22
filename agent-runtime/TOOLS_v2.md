# RxLocal tools — enforced order

1. `start_checkin(patient_id)` returns only a first name and verification ID.
2. `verify_identity(verification_id, provided_dob)` compares the patient's
   current answer, locks after two failures, and returns pass/fail plus an
   opaque verified-session token on success.
3. `get_verified_patient_meds(verified_session)` releases medication context
   only after backend verification and never returns DOB.
4. `remember_patient_fact` stores a durable patient-stated preference/barrier.
5. `log_outcome` records one truthful outcome with verbatim patient text.
6. `schedule_followup` always follows `log_outcome` using its recorded result.

Tool output and all opaque tokens are internal and never patient-facing.
