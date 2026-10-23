package runtime

func runEnroll(h Host, out *output) (object, error) {
	target := h.enrollment()
	c, err := enrollmentContext(metadata(pluginRoot(), target))
	if err != nil {
		return nil, err
	}
	credential, reason, err := obtainCredential(c, out, true)
	if err != nil {
		return nil, err
	}
	if credential == nil {
		if reason == "" {
			reason = "approval_pending"
		}
		return object{"status": "setup_pending", "reason": reason, "host": target}, nil
	}
	deployment := credential.DeploymentID
	if deployment == "" {
		deployment = c.DeploymentID
	}
	return object{"status": "enrolled", "host": target, "credential_id": nullable(credential.ID), "deployment_instance_id": deployment, "host_instance_id": c.HostID}, nil
}
func pendingNotices(h Host, out *output) {
	m := metadata(pluginRoot(), h)
	update := pendingUpdate(m)
	notice := ""
	if update != nil {
		notice = update.Notice
	}
	status, first := claimSuccess(h, "")
	welcome := claimWelcome(nil, false, m.PluginVersion, notice != "")
	if notice != "" || first != "" || welcome != "" {
		if status == "" {
			status = "notice"
		}
		out.emit(object{"status": status, "host": h, "needs_restart": status == "needs_restart"}, false, notice, first, welcome)
	}
	recordUpdate(update)
}
func ensure(h Host, quiet, ifSources, claim bool, out *output) error {
	if ifSources && !hasSources(h) {
		out.emit(object{"status": "trace_upload_skipped", "reason": "no_sources", "host": h}, quiet)
		return nil
	}
	h = h.enrollment()
	m := metadata(pluginRoot(), h)
	update := pendingUpdate(m)
	notice := ""
	if update != nil && claim {
		notice = update.Notice
	}
	c, err := enrollmentContext(m)
	if err != nil {
		return err
	}
	credential, reason, err := obtainCredential(c, out, quiet)
	if err != nil {
		return err
	}
	pending := func() {
		if reason == "" {
			reason = "approval_pending"
		}
		out.emit(object{"status": "setup_pending", "reason": reason, "host": h}, quiet, notice, claimWelcome(nil, quiet || !claim, m.PluginVersion, notice != ""))
	}
	if credential == nil {
		pending()
	} else {
		endpoint := c.Worker + "/v0/host-enrollment/policy?target=" + string(h)
		signed, err := requestJSON("GET", endpoint, credential, nil)
		if authError(err) {
			if err = forgetCredential(c); err != nil {
				return err
			}
			credential, reason, err = enrollCredential(c, out, quiet)
			if err != nil {
				return err
			}
			if credential == nil {
				pending()
				return nil
			}
			signed, err = requestJSON("GET", endpoint, credential, nil)
		}
		if err != nil {
			return err
		}
		policy, err := validatePolicy(signed, h)
		if err != nil {
			return err
		}
		credential.Internal = credential.Internal || internalIdentity(signed) || internalIdentity(obj(signed["policy"]))
		storeIdentity(c, credential)
		upload := c.Worker + "/v0/traces/batches"
		ledger := scopedLedger(ledgerPath(), c.Worker, c.DeploymentID, c.HostID, h)
		var result ConfigResult
		blocked := requiresNewer(policy.RequiredVersion)
		if blocked {
			result = blockedConfig(h, "bootstrap_upgrade_required", "Worker policy requires a newer Promptless host runtime", object{"required_bootstrap_version": policy.RequiredVersion, "bootstrap_version": Version}, upload, ledger)
		} else {
			result, err = ensureConfig(h, upload, ledger)
			if err != nil {
				return err
			}
		}
		if err = checkIn(c.Worker+"/v0/host-enrollment/check-ins", credential, h, m, policy, result); err != nil {
			return err
		}
		first := ""
		if claim && !quiet {
			_, first = claimSuccess(h, result.Status)
		} else if !claim {
			deferSuccess(h, result.Status)
		}
		payload := object{"status": result.Status, "host": h, "needs_restart": result.NeedsRestart}
		if blocked {
			payload["reason"] = "bootstrap_upgrade_required"
		}
		out.emit(payload, quiet, notice, first, claimWelcome(credential, quiet || !claim, m.PluginVersion, notice != ""))
	}
	if update != nil && !quiet && (claim || update.Notice == "") {
		recordUpdate(update)
	}
	return nil
}
