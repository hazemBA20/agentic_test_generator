def pretty_print_test_plans(test_plans: TestPlans)   -> None:
    """Pretty-print a list of TestPlan objects to the console."""
    
    category_icons = {
        "happy_path": "✅",
        "negative": "❌",
        "boundary": "⚠️",
    }

    print(f"\n{'='*70}")
    print(f"TEST PLANS ({len(test_plans)} total)")
    print(f"{'='*70}")

    for i, plan in enumerate(test_plans, start=1):
        icon = category_icons.get(plan.category, "•")
        print(f"\n[{i}] {icon} {plan.name}  ({plan.category})")
        print(f"    {plan.description}")
        print(f"    {plan.method} {plan.path}")

        if plan.request_body:
            print(f"    Request body:")
            for k, v in plan.request_body.items():
                print(f"      {k}: {v}")

        print(f"    Expected status: {plan.expected_status_code}")

        if plan.expected_response:
            print(f"    Expected response:")
            for k, v in plan.expected_response.items():
                print(f"      {k}: {v}")

    print(f"\n{'='*70}\n")