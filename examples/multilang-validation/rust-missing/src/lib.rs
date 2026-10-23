pub fn add(left: i32, right: i32) -> i32 {
    left - right
}

#[cfg(test)]
mod tests {
    use super::add;

    #[test]
    fn adds_values() {
        assert_eq!(add(2, 3), 5);
    }
}
